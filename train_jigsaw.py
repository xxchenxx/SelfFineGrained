import os
import uuid
import torch
import torch.nn as nn
import models
import argparse
import torchvision
import tensorboardX
import torchvision.datasets as datasets
import torchvision.transforms as transforms

from utils import split_image, combine_image
from ss import rotation, JigsawGenerator

torch.backends.cudnn.benchmark = True

parser = argparse.ArgumentParser(description='Selfie')
parser.add_argument('--data', metavar='DIR',
                    help='path to dataset')
parser.add_argument('--dataset', type=str, default="CUB")
parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet50',
                    help='model architecture: ')
parser.add_argument('-j', '--workers', default=8, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=1000, type=int, metavar='N',
                    help='number of steps of selfie')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=32, type=int,
                    metavar='N', help='mini-batch size (default: 256)')
parser.add_argument('--lr', '--learning-rate', default=0.001, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--lr-method', default='step', type=str,
                    help='method of learning rate')
parser.add_argument('--lr-params', default=[], dest='lr_params',nargs='*',type=float,
                    action='append', help='params of lr method')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=3e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--print-freq', '-p', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--gpu', default=None, type=int,
                    help='GPU id to use.')
parser.add_argument('--store-model-everyepoch', dest='store_model_everyepoch', action='store_true',
                    help='store checkpoint in every epoch')
parser.add_argument('--evaluation', action="store_true")
parser.add_argument('--resume', action="store_true")

parser.add_argument('--load-weights', default=None, type=str)
parser.add_argument('--task', type=str, default=uuid.uuid1())
parser.add_argument('--with-rotation', action="store_true")
parser.add_argument('--with-jigsaw', action="store_true")
parser.add_argument('--seperate-layer4', action="store_true")
parser.add_argument('--rotation-aug', action="store_true")

class fake:
    def step():
        pass

def main():
    global args, best_prec1, summary_writer, jigsaw

    jigsaw = JigsawGenerator(30)
    args = parser.parse_args()
    summary_writer = tensorboardX.SummaryWriter(os.path.join('logs', args.task))
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225])
    print(args)

    if args.dataset == 'CUB':
        traindir = os.path.join(args.data, 'train')
        valdir = os.path.join(args.data, 'val')
        train_transforms = transforms.Compose([
            transforms.Resize(448),
            transforms.CenterCrop(448),
            transforms.ToTensor(),
            normalize,
        ])
        val_transforms = transforms.Compose([
            transforms.Resize(448),
            transforms.CenterCrop(448),
            transforms.ToTensor(),
            normalize,
        ])
        num_classes = 200
        train_dataset = datasets.ImageFolder(traindir, train_transforms)
        val_dataset = datasets.ImageFolder(valdir, val_transforms)
    elif args.dataset == 'cifar':
        train_transforms = transforms.Compose([
            transforms.ToTensor(),
            normalize,
        ])
        val_transforms = transforms.Compose([
            transforms.ToTensor(),
            normalize,
        ])
        train_dataset = datasets.CIFAR10(args.data, True, train_transforms)
        val_dataset   = datasets.CIFAR10(args.data, False, val_transforms)
    else:
        raise NotImplementedError

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=True,
        num_workers=args.workers, pin_memory=True, drop_last = True)

    val_loader = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True, drop_last = True)

    model = torchvision.models.resnet50(pretrained = False)
    if args.dataset == 'cifar':
        model.conv1 = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=2, bias=False)
    
    model.fc = nn.Linear(2048, 30)

    criterion = nn.CrossEntropyLoss().cuda()
    if args.gpu is None:
        model = torch.nn.DataParallel(model)
        model = model.cuda()
    else:
        model = model.cuda()

    optimizer = torch.optim.SGD(model.parameters(), lr=args.lr,
                                momentum=0.9,
                                weight_decay=args.weight_decay)

    #scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, float(args.epochs))
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, [60, 150], 0.7)
    best_prec1 = 0

    if not os.path.exists(os.path.join('models', str(args.task))):
        os.mkdir(os.path.join('models', str(args.task)))

    for epoch in range(args.start_epoch, args.epochs):
        trainObj, top1 = train(train_loader, model, criterion, optimizer, scheduler, epoch)
        _,_ = train(val_loader, model, criterion, optimizer, fake, epoch)
        valObj, prec1 = val(val_loader, model, criterion)
        summary_writer.add_scalar("train_loss", trainObj, epoch)
        summary_writer.add_scalar("test_loss", valObj, epoch)
        summary_writer.add_scalar("train_acc", top1, epoch)
        summary_writer.add_scalar("test_acc", prec1, epoch)
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)
        if is_best:
            torch.save(
                {
                'epoch': epoch,
                'model_state': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'best_prec1': best_prec1,
                }, os.path.join('models', str(args.task), 'checkpoint.pth.tar'))
            torch.save(
                {
                'epoch': epoch,
                'model_state': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'best_prec1': best_prec1,
                }, os.path.join('models', str(args.task), 'model_best.pth.tar'))
        else:
            torch.save(
                {
                'epoch': epoch,
                'model_state': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'best_prec1': best_prec1,
                }, os.path.join('models', str(args.task), 'checkpoint.pth.tar'))

def train(train_loader, model, criterion, optimizer, scheduler, epoch):
    global args
    losses = AverageMeter()
    top1 = AverageMeter()
    model.train()
    for index, (input, target) in enumerate(train_loader):
        input = input.cuda(args.gpu)
        if args.dataset == 'CUB':
            splited_list = split_image(input, 112)
        elif args.dataset == 'cifar':
            splited_list = split_image(input, 8)
        splited_list = [i.unsqueeze(1) for i in splited_list]
        jigsaw_stacked = torch.cat(splited_list, 1)
        jigsaw_stacked, target = jigsaw(jigsaw_stacked)
        jigsaw_stacked = combine_image(jigsaw_stacked, 4)

        output = model(jigsaw_stacked)

        loss = criterion(output, target)
        
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        prec1 = accuracy(output, target, topk=(1,))
        losses.update(loss.item(), input.shape[0])
        top1.update(prec1[0].item(), input.shape[0])

        if index % args.print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'.format(
                   epoch, index, len(train_loader), loss=losses, top1=top1))
    scheduler.step()
    return losses.avg, top1.avg

def val(val_loader, model, criterion):
    global args
    losses = AverageMeter()
    top1 = AverageMeter()
    model.eval()
    with torch.no_grad():
        for index, (input, target) in enumerate(val_loader):
            input = input.cuda(args.gpu)
            if args.dataset == 'CUB':
                splited_list = split_image(input, 112)
            elif args.dataset == 'cifar':
                splited_list = split_image(input, 8)
            splited_list = [i.unsqueeze(1) for i in splited_list]
            jigsaw_stacked = torch.cat(splited_list, 1)
            jigsaw_stacked, target = jigsaw(jigsaw_stacked)
            jigsaw_stacked = combine_image(jigsaw_stacked, 4)

            output = model(jigsaw_stacked)
            loss = criterion(output, target)

            prec1 = accuracy(output, target, topk=(1,))
            losses.update(loss.item(), input.shape[0])
            top1.update(prec1[0].item(), input.shape[0])

            if index % args.print_freq == 0:
                print('Epoch: [{0}/{1}]\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'.format(
                       index, len(val_loader), loss=losses, top1=top1))

    return losses.avg, top1.avg

def accuracy(output, target, topk=(1,)):
    #print(output.shape)
    #print(target.shape)
    """Computes the precision@k for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)
        #print(target)
        if (target.dim() > 1):
            target = torch.argmax(target, 1)
        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res

def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, filename[0])
    if is_best:
        shutil.copyfile(filename[0], filename[1])

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

if __name__ == '__main__':
    main()