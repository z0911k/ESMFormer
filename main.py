import glob
import logging
import random
import torch.optim as optim
from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter

from common.utils import *
from common.opt import opts
from common.h36m_dataset import Human36mDataset
from common.Mydataset import Fusion
from common.computer_triangulate_loss import triangulate_loss
from common.computer_reprojection_loss import reprojection_loss


from model.esmformer import Model

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2,3"
CUDA_ID = [0, 1, 2, 3]
device = torch.device("cuda")


def train(opt, actions, train_loader, model, optimizer, epoch, writer, adaptive_weight):
    return step('train', opt, actions, train_loader, model, optimizer, epoch, writer, adaptive_weight)


def val(opt, actions, val_loader, model):
    with torch.no_grad():
        return step('test', opt, actions, val_loader, model)


def step(split, opt, actions, dataLoader, model, optimizer=None, epoch=None, writer=None, adaptive_weight=None):
    loss_all = {'loss': AccumLoss()}
    action_error_sum = define_error_list(actions)

    if split == 'train':
        model.train()
    else:
        model.eval()

    TQDM = tqdm(enumerate(dataLoader), total=len(dataLoader), ncols=100)
    for i, data in TQDM:
        batch_cam, gt_3D, input_2D, action, subject, scale, bb_box, start, end = data
        [input_2D, gt_3D, batch_cam, scale, bb_box] = get_varialbe(split, [input_2D, gt_3D, batch_cam, scale, bb_box])

        if split == 'train':
            output_3D = model(input_2D)
        elif split == 'test':
            input_2D, output_3D = input_augmentation(input_2D, model)

        out_target = gt_3D.clone()
        out_target[:, :, 0] = 0

        if split == 'train':

            if opt.self_supervised:  ## self_supervised
                if opt.tri_loss and not opt.reproj_loss:
                    loss_batch = triangulate_loss(output_3D[:, 0], input_2D[:, opt.pad], subject)
                elif not opt.tri_loss and opt.reproj_loss:
                    loss_batch = reprojection_loss(output_3D[:, 0], input_2D[:, opt.pad], subject, cam_ind)
                else:
                    loss_tri = triangulate_loss(output_3D[:, 0], input_2D[:, opt.pad], subject)
                    loss_reproj = reprojection_loss(output_3D[:, 0], input_2D[:, opt.pad], subject)
                    loss_batch = opt.loss_w * loss_tri + (1 - opt.loss_w) * loss_reproj

                if opt.adaptive_loss:  ## adaptive_loss
                    adaptive_weight, mean, std_var = update_adaptive_weight(adaptive_weight, subject, action, start,
                                                                            end, loss_batch)
                    temp_weight = get_adaptive_weight(adaptive_weight, subject, action, start, end)
                    temp_weight = temp_weight.to(device)
                    temp_weight_sum = torch.sum(temp_weight, dim=1)[0]
                    mean, std_var = torch.mean(loss_batch), torch.sqrt(torch.var(loss_batch))
                    if len(loss_batch.shape) == 1:
                        loss_batch = loss_batch.reshape(-1, 1).float()
                    loss = torch.mm(temp_weight, loss_batch.to(device)) / torch.sum(temp_weight, dim=1)

                else:
                    temp_weight_sum = torch.tensor(input_2D.size(0))
                    mean, std_var = torch.mean(loss_batch), torch.sqrt(torch.var(loss_batch))
                    loss = torch.mean(loss_batch.to(device))

                TQDM.set_description(f'Epoch [{epoch}/{opt.nepoch}]')
                TQDM.set_postfix({"l": loss.item(), "w": temp_weight_sum.item(),
                                  'u': mean.item(), 's': std_var.item()})

            else:
                loss = mpjpe_cal(output_3D, out_target)

                TQDM.set_description(f'Epoch [{epoch}/{opt.nepoch}]')
                TQDM.set_postfix({"l": loss.item()})

            N = input_2D.size(0)
            loss_all['loss'].update(loss.detach().cpu().numpy() * N, N)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            writer.add_scalars(main_tag='scalars1/train_loss',
                               tag_scalar_dict={'trianloss': loss.item()},
                               global_step=(epoch - 1) * len(dataLoader) + i)

        elif split == 'test':
            if output_3D.shape[1] != 1:
                output_3D = output_3D[:, opt.pad].unsqueeze(1)
            output_3D[:, :, 1:, :] -= output_3D[:, :, :1, :]
            output_3D[:, :, 0, :] = 0
            action_error_sum = test_calculation(output_3D, out_target, action, action_error_sum, opt.dataset, subject)

    if split == 'train':
        return loss_all['loss'].avg
    elif split == 'test':
        p1, p2 = print_error(opt.dataset, action_error_sum, opt.train)
        return p1, p2


def input_augmentation(input_2D, model):
    input_2D_non_flip = input_2D[:, 0]
    output_3D_non_flip = model(input_2D_non_flip)
    input_2D = input_2D_non_flip

    return input_2D, output_3D_non_flip


if __name__ == '__main__':
    opt = opts().parse()
    root_path = opt.root_path
    opt.manualSeed = 1
    random.seed(opt.manualSeed)
    torch.manual_seed(opt.manualSeed)

    if opt.train:
        logging.basicConfig(format='%(asctime)s %(message)s', datefmt='%Y/%m/%d %H:%M:%S',
                            filename=os.path.join(opt.checkpoint, 'train.log'), level=logging.INFO)

    root_path = opt.root_path
    dataset_path = root_path + 'data_3d_' + opt.dataset + '.npz'

    dataset = Human36mDataset(dataset_path, opt)
    actions = define_actions(opt.actions)

    if opt.train:
        train_data = Fusion(opt=opt, train=True, dataset=dataset, root_path=root_path)
        train_dataloader = torch.utils.data.DataLoader(train_data, batch_size=opt.batch_size,
                                                       shuffle=True, num_workers=int(opt.workers), pin_memory=True)

    test_data = Fusion(opt=opt, train=False, dataset=dataset, root_path=root_path)
    test_dataloader = torch.utils.data.DataLoader(test_data, batch_size=opt.batch_size,
                                                  shuffle=False, num_workers=int(opt.workers), pin_memory=True)

    model = Model(opt)
    if torch.cuda.device_count() > 1:
        print("Let's use", torch.cuda.device_count(), "GPUs!")
        model = torch.nn.DataParallel(model, device_ids=CUDA_ID).to(device)
        model = model.to(device)

    model_dict = model.state_dict()
    if opt.previous_dir != '':
        print('pretrained model path:', opt.previous_dir)
        model_path = opt.previous_dir

        pre_dict = torch.load(model_path)

        model_dict = model.state_dict()
        state_dict = {k: v for k, v in pre_dict.items() if k in model_dict.keys()}
        model_dict.update(state_dict)
        model.load_state_dict(model_dict)

    all_param = []
    lr = opt.lr
    all_param += list(model.parameters())

    optimizer = optim.Adam(all_param, lr=opt.lr, amsgrad=True)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min', factor=0.317, patience=5, verbose=True)

    ## tensorboard
    writer = SummaryWriter("runs/nin")
    flag = 0

    adaptive_weight = define_adaptive_weight()

    for epoch in range(1, opt.nepoch):
        if opt.train:
            loss = train(opt, actions, train_dataloader, model, optimizer, epoch, writer, adaptive_weight)

        p1, p2 = val(opt, actions, test_dataloader, model)

        if opt.train:
            save_model_epoch(opt.checkpoint, epoch, model)

            if p1 < opt.previous_best_threshold:
                opt.previous_name = save_model(opt.previous_name, opt.checkpoint, epoch, p1, model)
                opt.previous_best_threshold = p1

        if opt.train == 0:
            print('p1: %.2f, p2: %.2f' % (p1, p2))
            break
        else:
            logging.info('epoch: %d, lr: %.7f, loss: %.4f, p1: %.2f, p2: %.2f' % (epoch, lr, loss, p1, p2))
            print('e: %d, lr: %.7f, loss: %.4f, p1: %.2f, p2: %.2f' % (epoch, lr, loss, p1, p2))

        if epoch % opt.large_decay_epoch == 0:
            for param_group in optimizer.param_groups:
                param_group['lr'] *= opt.lr_decay_large
                lr *= opt.lr_decay_large
        else:
            for param_group in optimizer.param_groups:
                param_group['lr'] *= opt.lr_decay
                lr *= opt.lr_decay

    print(opt.checkpoint)
