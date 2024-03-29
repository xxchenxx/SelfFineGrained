import torch
import torchvision

import copy
import torch.nn as nn
import numpy as np

from utils import split_image

from resnetv2 import ResNet50 as resnet50v2
from attention_pooling.attention_pooling import SelfieModel
import pooling


from collections import OrderedDict

class Model(nn.Module):

    def __init__(self, args, num_classes = 200):
        super(Model, self).__init__()
        self.args = args
        if args.arch == 'resnet50v1':
            self.feature = torchvision.models.resnet50(pretrained = True)
        elif args.arch == 'resnet50v2':
            self.feature = resnet50v2(num_classes)
        else:
            raise NotImplementedError

        if args.pooling == 'avg':
            self.fc = nn.Linear(2048, num_classes)
        elif args.pooling == 'MPNCOV':
            self.avgpool = pooling.MPNCOV(5, True, True, 256, None)
            self.fc = nn.Linear(2048, num_classes)

        if args.dataset == 'cifar':
            self.feature.conv1 = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=2, bias=False)

        if args.with_rotation:
            self.rotation_fc = nn.Linear(2048, 4)
        else:
            self.rotation_fc = None

        if args.with_jigsaw:
            self.jigsaw_fc = nn.Linear(2048, 30)
        else:
            self.jigsaw_fc = None

        if args.with_selfie:
            if args.dataset == 'cifar':
                n_layers = 12
                n_split = 4
            elif args.dataset == 'imagenet224': 
                n_layers = 49 - 12
                n_split = 7
            elif args.dataset == 'CUB':
                n_layers = 49 - 12
                n_split = 7
            d_model = 1024 #vector length after the patch routed in P
            if args.dataset == 'CUB':
                d_model = 1024
            d_in = 128
            n_heads = d_model// d_in
            d_ff = 2048
            self.selfie = SelfieModel(n_layers, n_heads, d_in, d_model, d_ff, n_split, gpu=args.gpu, shared = False) 

        if args.load_weights is not None:
            try:
                state_dict = model.state_dict()
                load_state_dict = torch.load(args.load_weights)['P_state']
                state_dict.update(load_state_dict)
                model.load_state_dict(state_dict)

            except RuntimeError:
                model_loaded = torch.load(args.load_weights)
                data_dict = model_loaded['P_state']
                state_dict = model.state_dict()
                from collections import OrderedDict
                new_state_dict = OrderedDict()
                for k, v in data_dict.items():
                    name = k[7:] # remove `module.`
                    new_state_dict[name] = v
                new_state_dict.pop('conv1.weight', None)
                new_state_dict.pop('conv1.bias', None)
                state_dict.update(new_state_dict)
                model.load_state_dict(state_dict)
                del new_state_dict
                del data_dict

        if args.seperate_layer4:
            self.rotation_layer4_ = copy.deepcopy(self.feature.layer4)
            self.jigsaw_layer4_ = copy.deepcopy(self.feature.layer4)
        else:
            self.rotation_layer4_ = None
            self.jigsaw_layer4_ = None



    def extract_feature(self, x):
        x = self.feature.conv1(x)
        x = self.feature.bn1(x)
        x = self.feature.relu(x)
        x = self.feature.maxpool(x)
        x = self.feature.layer1(x)
        x = self.feature.layer2(x)
        x = self.feature.layer3(x)
        return x

    def layer4(self, x):
        x = self.feature.layer4(x)
        x = self.feature.avgpool(x)
        x = torch.flatten(x, 1)
        return x

    def rotation_layer4(self, x):
        x = self.rotation_layer4_(x)
        x = self.feature.avgpool(x)
        x = torch.flatten(x, 1)
        return x

    def jigsaw_layer4(self, x):
        x = self.jigsaw_layer4_(x)
        x = self.feature.avgpool(x)
        x = torch.flatten(x, 1)
        return x

    def forward(self, x, rotation_x = None, jigsaw_x = None, selfie_x = None):

        bs = x.shape[0]
        output_encoder = None
        features = None
        x = self.extract_feature(x)
        x = self.layer4(x)
        x = self.fc(x)
        if rotation_x is not None:
            rotation_x = self.extract_feature(rotation_x)
            if self.rotation_layer4_ is None:
                rotation_x = self.layer4(rotation_x)
            else:
                rotation_x = self.rotation_layer4(rotation_x)

            rotation_x = self.rotation_fc(rotation_x)

        if jigsaw_x is not None:
            jigsaw_x = self.extract_feature(jigsaw_x)
            if self.jigsaw_layer4_ is None:
                jigsaw_x = self.layer4(jigsaw_x)
            else:
                jigsaw_x = self.jigsaw_layer4(jigsaw_x)

            jigsaw_x = self.jigsaw_fc(jigsaw_x)

        if selfie_x is not None:
            batches, v, t = selfie_x
            pos = t
            v = torch.from_numpy(v).cuda(self.args.gpu)
            t = torch.from_numpy(np.array(pos)).cuda(self.args.gpu)


            input_encoder = batches.index_select(1, v)
            input_decoder = batches.index_select(1, t)
            output_decoder = []

            input_encoder = torch.split(input_encoder, 1, 1)
            input_encoder = list(map(lambda x: x.squeeze(1), input_encoder))
            input_encoder = torch.cat(input_encoder, 0)

            input_decoder = torch.split(input_decoder, 1, 1)
            input_decoder = list(map(lambda x: x.squeeze(1), input_decoder))
            input_decoder = torch.cat(input_decoder, 0)


            output_encoder = self.extract_feature(input_encoder)
            output_encoder = self.feature.avgpool(output_encoder).view(-1, 1024).unsqueeze(1)

            output_encoder = torch.split(output_encoder, bs, 0)
            output_encoder = torch.cat(output_encoder, 1)
            output_encoder = self.selfie(output_encoder, pos)

            output_decoder = self.extract_feature(input_decoder)
            output_decoder = self.feature.avgpool(output_decoder).view(-1, 1024).unsqueeze(1)
            output_decoder = torch.split(output_decoder, bs, 0)
            output_decoder = torch.cat(output_decoder, 1)
            features = []
            for i in range(len(pos)):
                feature = output_decoder[:, i, :]
                feature = feature.unsqueeze(2)
                features.append(feature)

            features = torch.cat(features, 2) # (B, F, NP)
        

        return x, rotation_x, jigsaw_x, (output_encoder, features)

def split_resnet50_layer3_forward(model, x):
    x = model.conv1(x)
    x = model.bn1(x)
    x = model.relu(x)
    x = model.maxpool(x)
    x = model.layer1(x)
    x = model.layer2(x)
    x = model.layer3(x)
    return x


class SelfEnsembleModel(nn.Module):

    def __init__(self, args, num_of_branches, num_classes = 200):

        super(SelfEnsembleModel, self).__init__()
        self.num_of_branches = num_of_branches
        self.args = args

        if args.arch == 'resnet50v1':
            self.branches = [torchvision.models.resnet50(pretrained = True) for _ in range(num_of_branches)]
            self.layer4 = torchvision.models.resnet50(pretrained = True).layer4 
        elif args.arch == 'resnet50v2':
            self.branches = [resnet50v2() for _ in range(num_of_branches)]
            self.layer4 = resnet50v2().layer4 

        
        if args.pooling == 'avg':
            self.avgpool = nn.AdaptiveAvgPool2d((1,1))
            self.fc = nn.Linear(2048, num_classes)
            self.layer_reduce = None
        elif args.pooling == 'MPNCOV':
            self.avgpool = pooling.MPNCOV()
            
            self.layer_reduce = nn.Conv2d(2048, 256, kernel_size=1, stride=1, padding=0,
                               bias=False)
            self.layer_reduce_bn = nn.BatchNorm2d(256)
            self.layer_reduce_relu = nn.ReLU(inplace=True)
            self.fc = nn.Linear(int(256*(256+1)/2), num_classes)

        self.gate = None # Will be initialized in forward()

        self.files = args.branches_enabled.split(',')
        self.files = list(map(lambda x: 'models/' + x + '_' + args.dataset + '/model_best.pth.tar', self.files))

        for i in self.branches:
            if args.dataset == 'cifar':
                i.conv1 = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=2, bias=False)
            i.cuda()

        self._load(self.files)


    def _load(self, files):
        for i in range(self.num_of_branches):
            if 'origin' in self.files[i]:
                origin_dict = torch.load(files[i])
                new_state_dict = OrderedDict()
                for k, v in origin_dict.items():
                    if (not k.startswith('fc')) and (not k.startswith('layer_reduce')):
                        new_state_dict[k] = v

                state_dict = self.branches[i].state_dict()
                state_dict.update(new_state_dict)
                self.branches[i].load_state_dict(state_dict)

                if self.layer_reduce is not None:
                    new_state_dict = OrderedDict()
                    for k, v in origin_dict.items():
                        if k.startswith('layer_reduce'):
                            new_state_dict[k] = v
                    for j in ['layer_reduce.', 'layer_reduce_bn.', 'layer_reduce_relu.']:
                        temp_state = OrderedDict()
                        for k, v in new_state_dict.items():
                            if k.startswith(j):
                                temp_state[k.split('.')[1]] = v

                        getattr(self, j[:-1]).load_state_dict(temp_state)

            else:
                try:
                    state_dict = self.branches[i].state_dict()
                    new_state_dict = torch.load(files[i])['model_state']
                    if 'fc.weight' in new_state_dict:
                        del new_state_dict['fc.weight']
                        del new_state_dict['fc.bias']
                    elif 'module.fc.weight' in new_state_dict:
                        del new_state_dict['module.fc.weight']
                        del new_state_dict['module.fc.bias']

                    if 'conv1.weight' in new_state_dict:
                        del new_state_dict['conv1.weight']
                    elif 'module.conv1.weight' in new_state_dict:
                        del new_state_dict['module.conv1.weight']

                    state_dict.update(new_state_dict)
                    self.branches[i].load_state_dict(state_dict)
                except RuntimeError as e:
                    state_dict = self.branches[i].state_dict()
                    model_loaded = torch.load(files[i])
                    data_dict = model_loaded['model_state']
                    new_state_dict = OrderedDict()
                    for k, v in data_dict.items():
                        name = k[7:] # remove `module.`
                        new_state_dict[name] = v
                    #print(state_dict.keys())
                    state_dict.update(new_state_dict)

                    self.branches[i].load_state_dict(state_dict)
                    del new_state_dict
                    del data_dict

        



    def forward(self, x):
        # x: List
        feature_maps = []
        
        if self.gate is None:
            self.gate = nn.Parameter(torch.ones(self.num_of_branches).cuda(self.args.gpu) * 1.0)

        #print(self.gate.grad)
        activated_gate = torch.softmax(self.gate, 0)
        for i in range(self.num_of_branches):
            feature_map = split_resnet50_layer3_forward(self.branches[i], x[i]).unsqueeze(0)
            feature_maps.append(feature_map * activated_gate[i])

        feature_maps = torch.sum(torch.cat(feature_maps, 0), 0)

        feature_maps = self.layer4(feature_maps)

        if self.layer_reduce is not None:
            feature_maps = self.layer_reduce(feature_maps)
            feature_maps = self.layer_reduce_bn(feature_maps)
            feature_maps = self.layer_reduce_relu(feature_maps)

        feature_maps = self.avgpool(feature_maps)
        feature_maps = torch.flatten(feature_maps, 1)

        feature_maps = self.fc(feature_maps)

        return feature_maps



        

        

