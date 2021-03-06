import argparse
import datetime
import logging
import os
import sys
import time
import traceback

import torch
import torch.backends.cudnn as cudnn
# option file should be modified according to your expriment
from options import Option
from torch.autograd import Variable
from torchvision import transforms

import ipytorch.models as md
import ipytorch.utils as utils
import ipytorch.visualization as vs
from ipytorch.utils.ifeige import IFeige, Notification
from ipytorch.checkpoint import CheckPoint
from ipytorch.dataloader import DataLoader
from ipytorch.trainer import Trainer
from torchlearning.mio.utils import bytes2image
from ipytorch.dataloader.preprocess import inception_color_preproccess

use_differeanble = False
if use_differeanble:
    from ipytorch.models.dqn.quantization_differentiable import QConv2d, QLinear, QReLU
else:
    from ipytorch.models.dqn.quantization import QConv2d, QLinear, QReLU


# from trainer import QTrainer


class ExperimentDesign:
    def __init__(self, options=None, conf_path=None):
        self.settings = options or Option(conf_path)
        self.checkpoint = None
        self.train_loader = None
        self.test_loader = None
        self.model = None

        self.optimizer_state = None
        self.trainer = None
        self.start_epoch = 0
        self.test_input = None

        os.environ['CUDA_DEVICE_ORDER'] = "PCI_BUS_ID"
#        os.environ['CUDA_VISIBLE_DEVICES'] = self.settings.visible_devices

        self.settings.set_save_path()
        self.logger = self.set_logger()
        self.settings.paramscheck(self.logger)
        self.visualize = vs.Visualization(self.settings.save_path, self.logger)
        self.tensorboard_logger = vs.Logger(self.settings.save_path)
        self.ifeige = IFeige()

        self.prepare()

    def set_logger(self):
        logger = logging.getLogger('baseline')
        file_formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
        console_formatter = logging.Formatter('%(message)s')
        # file log
        file_handler = logging.FileHandler(os.path.join(self.settings.save_path, "train_test.log"))
        file_handler.setFormatter(file_formatter)

        # console log
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(console_formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

        logger.setLevel(logging.INFO)
        return logger

    def prepare(self):
        self._set_gpu()
        self._set_dataloader()
        self._set_model()
        self._replace()
        self._set_checkpoint()
        # print(self.model.scalar)
        self.logger.info(self.model)
        # self.logger.info(self.model.scalar)
        self.logger.info(QConv2d)
        # assert False
        self._set_trainer()
        # self._draw_net()

    def _set_gpu(self):
        # set torch seed
        # init random seed
        # torch.backends.cudnn.deterministic = True
        torch.manual_seed(self.settings.manualSeed)
        torch.cuda.manual_seed(self.settings.manualSeed)
        assert self.settings.GPU <= torch.cuda.device_count() - 1, "Invalid GPU ID"
        # torch.cuda.set_device(self.settings.GPU)
        cudnn.benchmark = True

    def _set_dataloader(self):
        # create data loader
        data_loader = DataLoader(dataset=self.settings.dataset,
                                 batch_size=self.settings.batchSize,
                                 data_path=self.settings.dataPath,
                                 n_threads=self.settings.nThreads,
                                 ten_crop=self.settings.tenCrop,
                                 logger=self.logger)

        self.train_loader, self.test_loader = data_loader.getloader()

        if self.settings.netType == "AlexNet" and self.settings.dataset == "cifar100":
            if self.settings.dataset == "cifar10":
                norm_mean = [0.49139968, 0.48215827, 0.44653124]
                norm_std = [0.24703233, 0.24348505, 0.26158768]
            elif self.settings.dataset == "cifar100":
                norm_mean = [0.50705882, 0.48666667, 0.44078431]
                norm_std = [0.26745098, 0.25568627, 0.27607843]

            train_transform = transforms.Compose([
                transforms.RandomResizedCrop(224),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                transforms.Normalize(norm_mean, norm_std)])
            test_transform = transforms.Compose([
                transforms.Resize(224),
                transforms.ToTensor(),
                transforms.Normalize(norm_mean, norm_std)])

            self.train_loader.dataset.transform = train_transform
            self.test_loader.dataset.transform = test_transform
        # if self.settings.dataset == "imagenet":
        #     normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
        #                                      std=[0.229, 0.224, 0.225])

        #     train_transform = transforms.Compose([
        #         transforms.Resize(256),
        #         transforms.RandomCrop(224),
        #         transforms.RandomHorizontalFlip(),
        #         transforms.ToTensor(),
        #         normalize,
        #     ])
        #     test_transform = transforms.Compose([
        #         # bytes2image,
        #         # lambda image: image.convert('RGB'),
        #         transforms.Resize(256),
        #         transforms.CenterCrop(224),
        #         transforms.ToTensor(),
        #         normalize
        #     ])

        #     self.train_loader.dataset.transform = train_transform
        #     self.test_loader.dataset.transform = test_transform

    def _set_checkpoint(self):

        assert self.model is not None, "please create model first"

        self.checkpoint = CheckPoint(self.settings.save_path, self.logger)

        if self.settings.retrain is not None:
            model_state = self.checkpoint.load_model(self.settings.retrain)
            self.model = self.checkpoint.load_state(self.model, model_state)

        if self.settings.resume is not None:
            model_state, optimizer_state, epoch = self.checkpoint.load_checkpoint(
                self.settings.resume)
            self.model = self.checkpoint.load_state(self.model, model_state)
            self.start_epoch = epoch
            self.optimizer_state = optimizer_state

    def _set_model(self):
        if self.settings.dataset in ["cifar10", "cifar100"]:
            self.test_input = Variable(torch.randn(1, 3, 32, 32).cuda())
            if self.settings.netType == "PreResNet":
                self.model = md.official.PreResNet(depth=self.settings.depth,
                                                   num_classes=self.settings.nClasses,
                                                   wide_factor=self.settings.wideFactor)

            elif self.settings.netType == "PreResNet_Test":
                self.model = md.PreResNet_Test(depth=self.settings.depth,
                                               num_classes=self.settings.nClasses,
                                               wide_factor=self.settings.wideFactor,
                                               max_conv=10)

            elif self.settings.netType == "ResNet":
                self.model = md.custom.QuanResNetCifar(depth=self.settings.depth,
                                                   num_classes=self.settings.nClasses,
                                                   wide_factor=self.settings.wideFactor)

            elif self.settings.netType == "DenseNet_Cifar":
                self.model = md.official.DenseNet_Cifar(depth=self.settings.depth,
                                                        num_classes=self.settings.nClasses,
                                                        reduction=1.0,
                                                        bottleneck=False)
            elif self.settings.netType == "NetworkInNetwork":
                self.model = md.NetworkInNetwork()

            elif self.settings.netType == "AlexNet":
                self.test_input = torch.randn(1, 3, 224, 224).cuda()
                self.model = md.custom.AlexNetBNCifar(num_classes=self.settings.nClasses)

            elif self.settings.netType == "VGG":
                self.model = md.VGG_CIFAR(
                    self.settings.depth, num_classes=self.settings.nClasses)
            else:
                assert False, "use %s data while network is %s" % (
                    self.settings.dataset, self.settings.netType)

        elif self.settings.dataset == "mnist":
            self.test_input = Variable(torch.randn(1, 1, 28, 28).cuda())
            if self.settings.netType == "LeNet5":
                self.model = md.LeNet5()
            elif self.settings.netType == "LeNet500300":
                self.model = md.LeNet500300()
            else:
                assert False, "use mnist data while network is:" + self.settings.netType

        elif self.settings.dataset in ["imagenet", "imagenet100", "imagenet_mio"]:
            if self.settings.netType == "ResNet":
                self.model = md.custom.QuanResNet(self.settings.depth, self.settings.nClasses)
            elif self.settings.netType == "PreResNet":
                self.model = md.official.PreResNetImageNet(self.settings.depth, self.settings.nClasses)
            elif self.settings.netType == "resnet18":
                self.model = md.resnet18()
            elif self.settings.netType == "resnet34":
                self.model = md.resnet34()
            elif self.settings.netType == "resnet50":
                self.model = md.resnet50()
            elif self.settings.netType == "resnet101":
                self.model = md.resnet101()
            elif self.settings.netType == "resnet152":
                self.model = md.resnet152()
            elif self.settings.netType == "VGG":
                self.model = md.VGG(
                    depth=self.settings.depth, bn_flag=False, num_classes=self.settings.nClasses)
            elif self.settings.netType == "VGG_GAP":
                self.model = md.VGG_GAP(
                    depth=self.settings.depth, bn_flag=False, num_classes=self.settings.nClasses)
            elif self.settings.netType == "Inception3":
                self.model = md.Inception3(num_classes=self.settings.nClasses)
            elif self.settings.netType == "MobileNet_v2":
                self.model = md.MobileNet_v2(
                    num_classes=self.settings.nClasses,
                )  # wide_scale=1.4)
            else:
                assert False, "use %s data while network is%s" % (
                    self.settings.dataset, self.settings.netType)

            if self.settings.netType in ["InceptionResNetV2", "Inception3"]:
                self.test_input = Variable(torch.randn(1, 3, 299, 299).cuda())
            else:
                self.test_input = Variable(torch.randn(1, 3, 224, 224).cuda())
        else:
            assert False, "unsupport data set: " + self.settings.dataset

    def _set_trainer(self):
        # set lr master
        lr_master = utils.LRPolicy(self.settings.lr,
                                   self.settings.nEpochs,
                                   self.settings.lrPolicy)
        params_dict = {
            'power': self.settings.power,
            'step': self.settings.step,
            'end_lr': self.settings.endlr,
            'decay_rate': self.settings.decayRate
        }

        lr_master.set_params(params_dict=params_dict)
        # set trainer
        self.trainer = Trainer(
            model=self.model,
            train_loader=self.train_loader,
            test_loader=self.test_loader,
            lr_master=lr_master,
            settings=self.settings,
            logger=self.logger,
            tensorboard_logger=self.tensorboard_logger,
            opt_type=self.settings.opt_type,
            optimizer_state=self.optimizer_state,
            run_count=self.start_epoch)
        # self.trainer.reset_optimizer(opt_type="RMSProp")

    def _draw_net(self):
        if self.settings.drawNetwork:
            rand_output, _ = self.trainer.forward(self.test_input)
            self.visualize.save_network(rand_output)
            self.visualize.write_settings(self.settings)

    def _replace(self):
        if self.settings.netType == "PreResNet" and self.settings.dataset in ["cifar10", "cifar100"]:
            for module in self.model.modules():
                # if isinstance(module, (md.official.PreResNet)):
                # temp_conv = QConv2d(
                #     k=self.settings.quantization_k,
                #     in_channels=module.conv.in_channels,
                #     out_channels=module.conv.out_channels,
                #     kernel_size=module.conv.kernel_size,
                #     stride=module.conv.stride,
                #     padding=module.conv.padding,
                #     bias=(module.conv.bias is not None))
                # temp_conv.weight.data.copy_(module.conv.weight.data)
                # if module.conv.bias is not None:
                #     temp_conv.bias.data.copy_(module.conv.bias.data)
                # module.conv = temp_conv
                #
                # module.relu = QReLU(self.settings.quantization_k, module.relu.inplace)
                #
                # temp_fc = QLinear(
                #     k=self.settings.quantization_k,
                #     in_features=module.fc.in_features,
                #     out_features=module.fc.out_features,
                #     bias=(module.fc.bias is not None))
                # temp_fc.weight.data.copy_(module.fc.weight.data)
                # if module.fc.bias is not None:
                #     temp_fc.bias.data.copy_(module.fc.bias.data)
                # module.fc = temp_fc
                if isinstance(module, (md.official.PreBasicBlock)):
                    if module.downsample is not None:
                        temp_downsample = QConv2d(
                            k=self.settings.qw,
                            in_channels=module.downsample.in_channels,
                            out_channels=module.downsample.out_channels,
                            kernel_size=module.downsample.kernel_size,
                            stride=module.downsample.stride,
                            padding=module.downsample.padding,
                            bias=(module.downsample.bias is not None))
                        temp_downsample.weight.data.copy_(module.downsample.weight.data)
                        if module.downsample.bias is not None:
                            temp_downsample.bias.data.copy_(module.downsample.bias.data)
                        module.downsample = temp_downsample

                    temp_conv1 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv1.in_channels,
                        out_channels=module.conv1.out_channels,
                        kernel_size=module.conv1.kernel_size,
                        stride=module.conv1.stride,
                        padding=module.conv1.padding,
                        bias=(module.conv1.bias is not None))
                    temp_conv1.weight.data.copy_(module.conv1.weight.data)
                    if module.conv1.bias is not None:
                        temp_conv1.bias.data.copy_(module.conv1.bias.data)
                    module.conv1 = temp_conv1

                    temp_conv2 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv2.in_channels,
                        out_channels=module.conv2.out_channels,
                        kernel_size=module.conv2.kernel_size,
                        stride=module.conv2.stride,
                        padding=module.conv2.padding,
                        bias=(module.conv2.bias is not None))
                    temp_conv2.weight.data.copy_(module.conv2.weight.data)
                    if module.conv2.bias is not None:
                        temp_conv2.bias.data.copy_(module.conv2.bias.data)
                    module.conv2 = temp_conv2

                    module.relu1 = QReLU(self.settings.qa, module.relu1.inplace)
                    module.relu2 = QReLU(self.settings.qa, module.relu2.inplace)
        elif self.settings.netType == "PreResNet" and self.settings.dataset in ["imagenet", "imagenet_mio"]:
            for module in self.model.modules():
                if isinstance(module, md.official.PreBasicBlockImageNet):
                    if module.downsample is not None:
                        temp_downsample = QConv2d(
                            k=self.settings.qw,
                            in_channels=module.downsample[0].in_channels,
                            out_channels=module.downsample[0].out_channels,
                            kernel_size=module.downsample[0].kernel_size,
                            stride=module.downsample[0].stride,
                            padding=module.downsample[0].padding,
                            bias=(module.downsample[0].bias is not None))
                        temp_downsample.weight.data.copy_(module.downsample[0].weight.data)
                        if module.downsample[0].bias is not None:
                            temp_downsample.bias.data.copy_(module.downsample[0].bias.data)
                        module.downsample[0] = temp_downsample

                    temp_conv1 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv1.in_channels,
                        out_channels=module.conv1.out_channels,
                        kernel_size=module.conv1.kernel_size,
                        stride=module.conv1.stride,
                        padding=module.conv1.padding,
                        bias=(module.conv1.bias is not None))
                    temp_conv1.weight.data.copy_(module.conv1.weight.data)
                    if module.conv1.bias is not None:
                        temp_conv1.bias.data.copy_(module.conv1.bias.data)
                    module.conv1 = temp_conv1

                    temp_conv2 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv2.in_channels,
                        out_channels=module.conv2.out_channels,
                        kernel_size=module.conv2.kernel_size,
                        stride=module.conv2.stride,
                        padding=module.conv2.padding,
                        bias=(module.conv2.bias is not None))
                    temp_conv2.weight.data.copy_(module.conv2.weight.data)
                    if module.conv2.bias is not None:
                        temp_conv2.bias.data.copy_(module.conv2.bias.data)
                    module.conv2 = temp_conv2

                    module.relu1 = QReLU(self.settings.qa, module.relu1.inplace)
                    module.relu2 = QReLU(self.settings.qa, module.relu2.inplace)
                elif isinstance(module, md.official.PreBottleneckImageNet):
                    if module.downsample is not None:
                        temp_downsample = QConv2d(
                            k=self.settings.qw,
                            in_channels=module.downsample[0].in_channels,
                            out_channels=module.downsample[0].out_channels,
                            kernel_size=module.downsample[0].kernel_size,
                            stride=module.downsample[0].stride,
                            padding=module.downsample[0].padding,
                            bias=(module.downsample[0].bias is not None))
                        temp_downsample.weight.data.copy_(module.downsample[0].weight.data)
                        if module.downsample[0].bias is not None:
                            temp_downsample.bias.data.copy_(module.downsample[0].bias.data)
                        module.downsample[0] = temp_downsample

                    temp_conv1 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv1.in_channels,
                        out_channels=module.conv1.out_channels,
                        kernel_size=module.conv1.kernel_size,
                        stride=module.conv1.stride,
                        padding=module.conv1.padding,
                        bias=(module.conv1.bias is not None))
                    temp_conv1.weight.data.copy_(module.conv1.weight.data)
                    if module.conv1.bias is not None:
                        temp_conv1.bias.data.copy_(module.conv1.bias.data)
                    module.conv1 = temp_conv1

                    temp_conv2 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv2.in_channels,
                        out_channels=module.conv2.out_channels,
                        kernel_size=module.conv2.kernel_size,
                        stride=module.conv2.stride,
                        padding=module.conv2.padding,
                        bias=(module.conv2.bias is not None))
                    temp_conv2.weight.data.copy_(module.conv2.weight.data)
                    if module.conv2.bias is not None:
                        temp_conv2.bias.data.copy_(module.conv2.bias.data)
                    module.conv2 = temp_conv2

                    temp_conv3 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv3.in_channels,
                        out_channels=module.conv3.out_channels,
                        kernel_size=module.conv3.kernel_size,
                        stride=module.conv3.stride,
                        padding=module.conv3.padding,
                        bias=(module.conv3.bias is not None))
                    temp_conv3.weight.data.copy_(module.conv3.weight.data)
                    if module.conv3.bias is not None:
                        temp_conv3.bias.data.copy_(module.conv3.bias.data)
                    module.conv3 = temp_conv3

                    module.relu1 = QReLU(self.settings.qa, module.relu1.inplace)
                    module.relu2 = QReLU(self.settings.qa, module.relu2.inplace)
                    module.relu3 = QReLU(self.settings.qa, module.relu3.inplace)
        elif self.settings.netType == "ResNet" and self.settings.dataset in ["imagenet", "imagenet_mio"]:
            for module in self.model.modules():
                if isinstance(module, (md.custom.QuanResNet)):
                    # temp_conv = QConv2d(
                    #     k=self.settings.qw,
                    #     in_channels=module.conv1.in_channels,
                    #     out_channels=module.conv1.out_channels,
                    #     kernel_size=module.conv1.kernel_size,
                    #     stride=module.conv1.stride,
                    #     padding=module.conv1.padding,
                    #     bias=(module.conv1.bias is not None))
                    # temp_conv.weight.data.copy_(module.conv1.weight.data)
                    # if module.conv1.bias is not None:
                    #     temp_conv.bias.data.copy_(module.conv1.bias.data)
                    # module.conv1 = temp_conv

                    module.relu = QReLU(self.settings.qa, module.relu.inplace)

                    # temp_fc = QLinear(
                    #     k=self.settings.qw,
                    #     in_features=module.fc.in_features,
                    #     out_features=module.fc.out_features,
                    #     bias=(module.fc.bias is not None))
                    # temp_fc.weight.data.copy_(module.fc.weight.data)
                    # if module.fc.bias is not None:
                    #     temp_fc.bias.data.copy_(module.fc.bias.data)
                    # module.fc = temp_fc
                if isinstance(module, (md.custom.QuanBasicBlock)):
                    if module.downsample is not None:
                        temp_downsample = QConv2d(
                            k=self.settings.qw,
                            in_channels=module.downsample[0].in_channels,
                            out_channels=module.downsample[0].out_channels,
                            kernel_size=module.downsample[0].kernel_size,
                            stride=module.downsample[0].stride,
                            padding=module.downsample[0].padding,
                            bias=(module.downsample[0].bias is not None))
                        temp_downsample.weight.data.copy_(module.downsample[0].weight.data)
                        if module.downsample[0].bias is not None:
                            temp_downsample.bias.data.copy_(module.downsample[0].bias.data)
                        module.downsample[0] = temp_downsample

                    temp_conv1 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv1.in_channels,
                        out_channels=module.conv1.out_channels,
                        kernel_size=module.conv1.kernel_size,
                        stride=module.conv1.stride,
                        padding=module.conv1.padding,
                        bias=(module.conv1.bias is not None))
                    temp_conv1.weight.data.copy_(module.conv1.weight.data)
                    if module.conv1.bias is not None:
                        temp_conv1.bias.data.copy_(module.conv1.bias.data)
                    module.conv1 = temp_conv1

                    temp_conv2 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv2.in_channels,
                        out_channels=module.conv2.out_channels,
                        kernel_size=module.conv2.kernel_size,
                        stride=module.conv2.stride,
                        padding=module.conv2.padding,
                        bias=(module.conv2.bias is not None))
                    temp_conv2.weight.data.copy_(module.conv2.weight.data)
                    if module.conv2.bias is not None:
                        temp_conv2.bias.data.copy_(module.conv2.bias.data)
                    module.conv2 = temp_conv2

                    module.relu1 = QReLU(self.settings.qa, module.relu1.inplace)
                    # module.relu2 = QReLU(self.settings.qa, module.relu2.inplace)
                    if not module.is_last:
                        module.relu2 = QReLU(self.settings.qa, module.relu2.inplace)
                elif isinstance(module, (md.custom.QuanBottleneck)):
                    if module.downsample is not None:
                        temp_downsample = QConv2d(
                            k=self.settings.qw,
                            in_channels=module.downsample[0].in_channels,
                            out_channels=module.downsample[0].out_channels,
                            kernel_size=module.downsample[0].kernel_size,
                            stride=module.downsample[0].stride,
                            padding=module.downsample[0].padding,
                            bias=(module.downsample[0].bias is not None))
                        temp_downsample.weight.data.copy_(module.downsample[0].weight.data)
                        if module.downsample[0].bias is not None:
                            temp_downsample.bias.data.copy_(module.downsample[0].bias.data)
                        module.downsample[0] = temp_downsample

                    temp_conv1 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv1.in_channels,
                        out_channels=module.conv1.out_channels,
                        kernel_size=module.conv1.kernel_size,
                        stride=module.conv1.stride,
                        padding=module.conv1.padding,
                        bias=(module.conv1.bias is not None))
                    temp_conv1.weight.data.copy_(module.conv1.weight.data)
                    if module.conv1.bias is not None:
                        temp_conv1.bias.data.copy_(module.conv1.bias.data)
                    module.conv1 = temp_conv1

                    temp_conv2 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv2.in_channels,
                        out_channels=module.conv2.out_channels,
                        kernel_size=module.conv2.kernel_size,
                        stride=module.conv2.stride,
                        padding=module.conv2.padding,
                        bias=(module.conv2.bias is not None))
                    temp_conv2.weight.data.copy_(module.conv2.weight.data)
                    if module.conv2.bias is not None:
                        temp_conv2.bias.data.copy_(module.conv2.bias.data)
                    module.conv2 = temp_conv2

                    temp_conv3 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv3.in_channels,
                        out_channels=module.conv3.out_channels,
                        kernel_size=module.conv3.kernel_size,
                        stride=module.conv3.stride,
                        padding=module.conv3.padding,
                        bias=(module.conv3.bias is not None))
                    temp_conv3.weight.data.copy_(module.conv3.weight.data)
                    if module.conv3.bias is not None:
                        temp_conv3.bias.data.copy_(module.conv3.bias.data)
                    module.conv3 = temp_conv3

                    module.relu1 = QReLU(self.settings.qa, module.relu1.inplace)
                    module.relu2 = QReLU(self.settings.qa, module.relu2.inplace)
                    # module.relu3 = QReLU(self.settings.qa, module.relu3.inplace)
                    if not module.is_last:
                        module.relu3 = QReLU(self.settings.qa, module.relu3.inplace)
        elif self.settings.netType == "ResNet" and self.settings.dataset in ["cifar10", "cifar100"]:
            for module in self.model.modules():
                if isinstance(module, (md.custom.QuanResidualBlockCifar)):
                    if module.down_sample is not None:
                        temp_down_sample = QConv2d(
                            k=self.settings.qw,
                            in_channels=module.down_sample[0].in_channels,
                            out_channels=module.down_sample[0].out_channels,
                            kernel_size=module.down_sample[0].kernel_size,
                            stride=module.down_sample[0].stride,
                            padding=module.down_sample[0].padding,
                            bias=(module.down_sample[0].bias is not None))
                        temp_down_sample.weight.data.copy_(module.down_sample[0].weight.data)
                        if module.down_sample[0].bias is not None:
                            temp_down_sample.bias.data.copy_(module.down_sample[0].bias.data)
                        module.down_sample = temp_down_sample

                    temp_conv1 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv1.in_channels,
                        out_channels=module.conv1.out_channels,
                        kernel_size=module.conv1.kernel_size,
                        stride=module.conv1.stride,
                        padding=module.conv1.padding,
                        bias=(module.conv1.bias is not None))
                    temp_conv1.weight.data.copy_(module.conv1.weight.data)
                    if module.conv1.bias is not None:
                        temp_conv1.bias.data.copy_(module.conv1.bias.data)
                    module.conv1 = temp_conv1

                    temp_conv2 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module.conv2.in_channels,
                        out_channels=module.conv2.out_channels,
                        kernel_size=module.conv2.kernel_size,
                        stride=module.conv2.stride,
                        padding=module.conv2.padding,
                        bias=(module.conv2.bias is not None))
                    temp_conv2.weight.data.copy_(module.conv2.weight.data)
                    if module.conv2.bias is not None:
                        temp_conv2.bias.data.copy_(module.conv2.bias.data)
                    module.conv2 = temp_conv2

                    module.relu1 = QReLU(self.settings.qa, module.relu1.inplace)
                    if not module.is_last:
                        module.relu2 = QReLU(self.settings.qa, module.relu2.inplace)

        elif self.settings.netType == "AlexNet":
            for module in self.model.modules():
                if isinstance(module, torch.nn.Sequential) and len(module) == 18:
                    # temp_conv1 = QConv2d(
                    #     k=self.settings.quantization_k,
                    #     in_channels=module[0].in_channels,
                    #     out_channels=module[0].out_channels,
                    #     kernel_size=module[0].kernel_size,
                    #     stride=module[0].stride,
                    #     padding=module[0].padding,
                    #     bias=(module[0].bias is not None))
                    # temp_conv1.weight.data.copy_(module[0].weight.data)
                    # if module[0].bias is not None:
                    #     temp_conv1.bias.data.copy_(module[0].bias.data)
                    # module[0] = temp_conv1
                    module[2] = QReLU(self.settings.qa, module[2].inplace)
                    # module[2] = ClipReLU(module[2].inplace)

                    temp_conv2 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module[4].in_channels,
                        out_channels=module[4].out_channels,
                        kernel_size=module[4].kernel_size,
                        stride=module[4].stride,
                        padding=module[4].padding,
                        bias=(module[4].bias is not None))
                    temp_conv2.weight.data.copy_(module[4].weight.data)
                    if module[4].bias is not None:
                        temp_conv2.bias.data.copy_(module[4].bias.data)
                    module[4] = temp_conv2
                    module[6] = QReLU(self.settings.qa, module[6].inplace)
                    # module[6] = ClipReLU(module[6].inplace)

                    temp_conv3 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module[8].in_channels,
                        out_channels=module[8].out_channels,
                        kernel_size=module[8].kernel_size,
                        stride=module[8].stride,
                        padding=module[8].padding,
                        bias=(module[8].bias is not None))
                    temp_conv3.weight.data.copy_(module[8].weight.data)
                    if module[8].bias is not None:
                        temp_conv3.bias.data.copy_(module[8].bias.data)
                    module[8] = temp_conv3
                    module[10] = QReLU(self.settings.qa, module[10].inplace)
                    # module[10] = ClipReLU(module[10].inplace)

                    temp_conv4 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module[11].in_channels,
                        out_channels=module[11].out_channels,
                        kernel_size=module[11].kernel_size,
                        stride=module[11].stride,
                        padding=module[11].padding,
                        bias=(module[11].bias is not None))
                    temp_conv4.weight.data.copy_(module[11].weight.data)
                    if module[11].bias is not None:
                        temp_conv4.bias.data.copy_(module[11].bias.data)
                    module[11] = temp_conv4
                    module[13] = QReLU(self.settings.qa, module[13].inplace)
                    # module[13] = ClipReLU(module[13].inplace)

                    temp_conv5 = QConv2d(
                        k=self.settings.qw,
                        in_channels=module[14].in_channels,
                        out_channels=module[14].out_channels,
                        kernel_size=module[14].kernel_size,
                        stride=module[14].stride,
                        padding=module[14].padding,
                        bias=(module[14].bias is not None))
                    temp_conv5.weight.data.copy_(module[14].weight.data)
                    if module[14].bias is not None:
                        temp_conv5.bias.data.copy_(module[14].bias.data)
                    module[14] = temp_conv5
                    module[16] = QReLU(self.settings.qa, module[16].inplace)
                    # module[16] = ClipReLU(module[16].inplace)
                elif isinstance(module, torch.nn.Sequential) and len(module) == 7:
                    temp_fc1 = QLinear(
                        k=self.settings.qw,
                        in_features=module[0].in_features,
                        out_features=module[0].out_features,
                        bias=(module[0].bias is not None))
                    temp_fc1.weight.data.copy_(module[0].weight.data)
                    if module[0].bias is not None:
                        temp_fc1.bias.data.copy_(module[0].bias.data)
                    module[0] = temp_fc1
                    module[2] = QReLU(self.settings.qa, module[2].inplace)
                    # module[2] = ClipReLU(module[2].inplace)

                    temp_fc2 = QLinear(
                        k=self.settings.qw,
                        in_features=module[3].in_features,
                        out_features=module[3].out_features,
                        bias=(module[3].bias is not None))
                    temp_fc2.weight.data.copy_(module[3].weight.data)
                    if module[3].bias is not None:
                        temp_fc2.bias.data.copy_(module[3].bias.data)
                    module[3] = temp_fc2
                    # module[5] = QReLU(self.settings.quantization_k, module[5].inplace)
                    # module[5] = ClipReLU(module[5].inplace)

                    # temp_fc3 = QLinear(
                    #     k=self.settings.quantization_k,
                    #     in_features=module[6].in_features,
                    #     out_features=module[6].out_features,
                    #     bias=(module[6].bias is not None))
                    # temp_fc3.weight.data.copy_(module[6].weight.data)
                    # if module[6].bias is not None:
                    #     temp_fc3.bias.data.copy_(module[6].bias.data)
                    # module[6] = temp_fc3

    def _model_analyse(self, model):
        # analyse model
        model_analyse = utils.ModelAnalyse(model, self.visualize)
        params_num = model_analyse.params_count()
        zero_num = model_analyse.zero_count()
        zero_rate = zero_num * 1.0 / params_num
        self.logger.info("zero rate is: {}".format(zero_rate))

        # save analyse result to file
        self.visualize.write_readme(
            "Number of parameters is: %d, number of zeros is: %d, zero rate is: %f" % (params_num, zero_num, zero_rate))

        # model_analyse.flops_compute(self.test_input)
        print(self.test_input.shape)
        model_analyse.madds_compute(self.test_input)

    def run(self, run_count=0):
        best_top1 = 100
        best_top5 = 100
        start_time = time.time()

        self._model_analyse(self.model)

        # test_error, test_loss, test5_error = self.trainer.test(0)
        # assert False
        try:
            for epoch in range(self.start_epoch, self.settings.nEpochs):
                self.epoch = epoch
                self.start_epoch = 0
                # training and testing
                train_error, train_loss, train5_error = self.trainer.train(
                    epoch=epoch)
                test_error, test_loss, test5_error = self.trainer.test(
                    epoch=epoch)
                # self.trainer.model.apply(utils.SVB)
                # self.trainer.model.apply(utils.BBN)

                # write and print result
                log_str = "%d\t%.4f\t%.4f\t%.4f\t%.4f\t%.4f\t%.4f\t" % (
                    epoch, train_error, train_loss, test_error, test_loss, train5_error, test5_error)

                self.visualize.write_log(log_str)
                best_flag = False
                if best_top1 >= test_error:
                    best_top1 = test_error
                    best_top5 = test5_error
                    best_flag = True

                self.logger.info("#==>Best Result is: Top1 Error: {:f}, Top5 Error: {:f}".format(best_top1, best_top5))
                self.logger.info("#==>Best Result is: Top1 Accuracy: {:f}, Top5 Accuracy: {:f}".format(100 - best_top1,
                                                                                                       100 - best_top5))

                if self.settings.dataset in ["imagenet", "imagenet100", "imagenet_mio"]:
                    self.checkpoint.save_checkpoint(
                        self.model, self.trainer.optimizer, epoch, epoch)
                else:
                    self.checkpoint.save_checkpoint(
                        self.model, self.trainer.optimizer, epoch)

                if best_flag:
                    self.checkpoint.save_model(self.model, best_flag=best_flag)

                # if (epoch + 1) % self.settings.drawInterval == 0:
                #     self.visualize.draw_curves()
        except BaseException as e:
            self.logger.error("Training is terminating due to exception: {}".format(str(e)))
            traceback.print_exc()
            self.checkpoint.save_checkpoint(
                self.model, self.trainer.optimizer, self.epoch, self.epoch)

        end_time = time.time()
        time_interval = end_time - start_time
        t_string = "Running Time is: " + \
                   str(datetime.timedelta(seconds=time_interval)) + "\n"
        self.logger.info(t_string)

        self.visualize.write_settings(self.settings)
        # save experimental results
        self.visualize.write_readme(
            "Best Result of all is: Top1 Error: {:f}, Top5 Error: {:f}\n".format(best_top1, best_top5))
        self.visualize.write_readme(
            "Best Result of all is: Top1 Accuracy: {:f}, Top5 Accuracy: {:f}\n".format(100 - best_top1,
                                                                                       100 - best_top5))
        self.ifeige.send_msg_to_user(
            username="Key",
            key=Notification.NOTICE,
            title="{} experiment complete\n".format(self.settings.experimentID),
            content="Top1 Accuracy: {:f}, Top5 Accuracy: {:f}".format(100 - best_top1, 100 - best_top5),
            remark=""
        )

        # self.visualize.draw_curves()

        # analyse model
        self._model_analyse(self.model)
        return best_top1, best_top5


# ---------------------------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description='Baseline')
    parser.add_argument('--conf_path', type=str, metavar='conf_path',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--id', type=int, metavar='experiment_id',
                        help='Experiment ID')
    args = parser.parse_args()

    option = Option(args.conf_path)
    option.manualSeed = args.id + 1
    option.experimentID = option.experimentID + "{:0>2d}_repeat".format(args.id + 1)

    experiment = ExperimentDesign(option)
    experiment.run()


if __name__ == '__main__':
    main()
