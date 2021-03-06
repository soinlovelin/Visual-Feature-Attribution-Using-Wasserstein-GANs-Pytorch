from __future__ import print_function
import os
import shutil

import torch
import numpy as np
import torch.optim as optim
import torchvision.utils as vutils
import torch.backends.cudnn as cudnn
import torchvision.transforms as transforms

from torch import nn
from torch import autograd
from torch.autograd import Variable


from parser import get_parser
from models.critics import C3DFCN
from models.mask_generators import UNet
from synth_dataset import SynthDataset

import matplotlib.pyplot as plt

LAMBDA_NORM = 100
LAMBDA = 10


def init_seed(opt):
    '''
    Disable cudnn to maximize reproducibility
    '''
    torch.cuda.cudnn_enabled = False
    torch.manual_seed(opt.manual_seed)
    torch.cuda.manual_seed(opt.manual_seed)
    cudnn.benchmark = True


def init_experiment(opt):
    if opt.experiment is None:
        opt.experiment = '../samples'
    try:
        shutil.rmtree(opt.experiment)
    except:
        pass
    os.makedirs(opt.experiment)


def weights_init(m):
    '''
    Initialize cnn weithgs.
    '''
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data = torch.nn.init.kaiming_normal(m.weight.data, 2)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)


def init_model(opt):
    '''
    Initialize generator and disciminator
    '''
    net_g = UNet(nf=opt.num_filters_g)
    net_g = nn.Sequential(net_g, nn.Tanh())
    net_d = C3DFCN(opt.channels_number, opt.num_filters_d)
    return net_g, net_d


def init_optimizer(opt, net_g, net_d):
    '''
    Initialize optimizers
    TODO use options for beta2 and wd
    '''
    optimizer_g = optim.Adam(net_g.parameters(), lr=opt.learning_rate_g, betas=(
        opt.beta1, 0.9), weight_decay=1e-5)
    optimizer_d = optim.Adam(net_d.parameters(), lr=opt.learning_rate_d, betas=(
        opt.beta1, 0.9), weight_decay=1e-5)
    return optimizer_g, optimizer_d


def init_synth_dataloader(opt, anomaly, mode='train'):
    '''
    Initialize both datasets and dataloaders
    '''
    dataset = SynthDataset(opt=opt, anomaly=anomaly,
                           mode=mode,
                           transform=transforms.Compose([
                               torch.FloatTensor,

                           ]))

    dataloader = torch.utils.data.DataLoader(dataset, batch_size=opt.batch_size,
                                             shuffle=True, drop_last=True)

    return dataloader


def calc_gradient_penalty(netD, real_data, fake_data):
    '''
    Calculate gradient penalty as in  "Improved Training of Wasserstein GANs"
    https://github.com/caogang/wgan-gp
    '''
    bs, ch, h, w = real_data.shape

    use_cuda = real_data.is_cuda
    alpha = torch.rand(bs, 1)
    alpha = alpha.expand(bs, int(real_data.nelement() /
                         bs)).contiguous().view(bs, ch, h, w)
    alpha = alpha.cuda() if use_cuda else alpha

    interpolates = alpha * real_data + ((1 - alpha) * fake_data)

    if use_cuda:
        interpolates = interpolates.cuda()
    interpolates = autograd.Variable(interpolates, requires_grad=True)

    disc_interpolates = netD(interpolates)

    gradients = autograd.grad(outputs=disc_interpolates, inputs=interpolates,
                              grad_outputs=torch.ones(disc_interpolates.size()).cuda() if use_cuda else torch.ones(
                                  disc_interpolates.size()),
                              create_graph=True, retain_graph=True, only_inputs=True)[0]
    gradients = gradients.view(gradients.size(0), -1)

    gradient_penalty = ((gradients.norm(2, dim=1) - 1) ** 2).mean() * LAMBDA
    return gradient_penalty


def train(opt, healthy_dataloader, anomaly_dataloader, net_g, net_d, optim_g, optim_d):
    '''
    Run the trainig algorithm.
    '''
    model_input = torch.FloatTensor(
        opt.batch_size, 1, opt.image_size, opt.image_size)
    fixed_model_input = iter(anomaly_dataloader).next()[0]

    if opt.cuda:
        fixed_model_input = fixed_model_input.cuda()

    vutils.save_image(fixed_model_input.mul(0.5).add(
        0.5), '{0}/real_samples.png'.format(opt.experiment))

    one = torch.FloatTensor([1])
    mone = one * -1

    if opt.cuda:
        fixed_model_input = fixed_model_input.cuda()
        one, mone = one.cuda(), mone.cuda()
        model_input = model_input.cuda()

    gen_iterations = 0
    for epoch in range(opt.nepochs):
        data_iter = iter(healthy_dataloader)
        anomaly_data_iter = iter(anomaly_dataloader)
        i = 0
        while i < len(anomaly_dataloader):
            ############################
            # (1) Update D network
            ###########################
            for p in net_d.parameters():  # reset requires_grad
                p.requires_grad = True  # they are set to False below in net_g update

            # train the discriminator d_iters times
            if gen_iterations < 25 or gen_iterations % 100 == 0:
                d_iters = 100
                print('Doing **critic** update steps ({} steps)'.format(d_iters))
            else:
                d_iters = opt.d_iters
            j = 0
            while j < d_iters and i < len(anomaly_dataloader):
                j += 1

                data = data_iter.next()
                i += 1

                # train with real / healthy data
                real_cpu = data[0]
                net_d.zero_grad()

                if opt.cuda:
                    real_cpu = real_cpu.cuda()
                model_input.resize_as_(real_cpu).copy_(real_cpu)
                inputv = Variable(model_input)
                err_d_real = net_d(inputv)

                # train with sum (anomalous + anomaly map)
                data = anomaly_data_iter.next()

                anomaly_cpu = data[0]
                net_d.zero_grad()

                if opt.cuda:
                    anomaly_cpu = anomaly_cpu.cuda()
                model_input.resize_as_(anomaly_cpu).copy_(anomaly_cpu)
                inputv = Variable(model_input)

                anomaly_map = net_g(inputv)

                outputv = anomaly_map
                img_sum = inputv + outputv

                err_d_anomaly_map = net_d(img_sum)

                cri_loss = err_d_real.mean() - err_d_anomaly_map.mean()
                cri_loss += calc_gradient_penalty(net_d, model_input, img_sum.data)

                cri_loss.backward()

                err_d = err_d_real - err_d_anomaly_map
                optim_d.step()

            ############################
            # (2) Update G network
            ###########################
            for p in net_d.parameters():
                p.requires_grad = False  # to avoid computation
            net_g.zero_grad()

            anomaly_map = net_g(inputv)

            # minimize the l1 norm for the anomaly map
            gen_loss = net_d(inputv + anomaly_map).mean()
            err_g = gen_loss

            gen_loss += torch.abs(anomaly_map).mean() * LAMBDA_NORM
            gen_loss.backward()

            optim_g.step()
            gen_iterations += 1

            print('[%d/%d][%d/%d][%d] Loss_D: %f Loss_G: %f Loss_D_real: %f Loss_D_diff %f'
                  % (epoch, opt.nepochs, i, len(healthy_dataloader), gen_iterations,
                     err_d.data[0], err_g.data[0], err_d_real.data[0], err_d_anomaly_map.data[0]))

            # print and save
            if gen_iterations % 50 == 0:
                anomaly_map = net_g(Variable(fixed_model_input, requires_grad=False))
                inp = np.vstack(np.hsplit(np.hstack(fixed_model_input[:, 0]), 4))
                img = np.vstack(np.hsplit(np.hstack(anomaly_map.data[:, 0]), 4))
                path = '{:}/fake_samples_{:05d}.png'.format(opt.experiment, gen_iterations)
                plt.imsave(path, -img, cmap='gray')
                path = '{:}/sum_samples_{:05d}.png'.format(opt.experiment, gen_iterations)
                plt.imsave(path, inp + img, cmap='gray')

        # do checkpointing
        torch.save(net_g.state_dict(),
                   '{0}/net_g_chckp.pth'.format(opt.experiment, epoch))
        torch.save(net_d.state_dict(),
                   '{0}/net_d_chckp.pth'.format(opt.experiment, epoch))

    # save models at last iteration
    torch.save(net_g.state_dict(),
               '{0}/net_g_chckp_last.pth'.format(opt.experiment, epoch))
    torch.save(net_d.state_dict(),
               '{0}/net_d_chckp_last.pth'.format(opt.experiment, epoch))


def main():
    options = get_parser().parse_args()

    if torch.cuda.is_available() and not options.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")

    init_experiment(options)
    init_seed(options)

    healthy_dataloader_train = init_synth_dataloader(
        options, anomaly=False, mode='train')
    healthy_dataloader_val = init_synth_dataloader(
        options, anomaly=False, mode='val')
    healthy_dataloader_test = init_synth_dataloader(
        options, anomaly=False, mode='test')

    anomaly_dataloader_train = init_synth_dataloader(
        options, anomaly=True, mode='train')
    anomaly_dataloader_val = init_synth_dataloader(
        options, anomaly=True, mode='val')
    anomaly_dataloader_test = init_synth_dataloader(
        options, anomaly=True, mode='test')

    net_g, net_d = init_model(options)

    net_g.apply(weights_init)
    net_d.apply(weights_init)

    optim_g, optim_d = init_optimizer(options, net_g=net_g, net_d=net_d)

    if options.cuda:
        net_g = net_g.cuda()
        net_d = net_d.cuda()

    train(options,
          healthy_dataloader_train, anomaly_dataloader_train,
          net_g=net_g, net_d=net_d,
          optim_g=optim_g, optim_d=optim_d)


if __name__ == '__main__':
    main()
