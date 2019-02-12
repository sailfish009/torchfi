import argparse
import os
import random
import shutil
import time
import warnings
import sys

import numpy as np

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.multiprocessing as mp
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models


import torchFI as tfi
from torchFI.injection import FI
from util.log import *

# import ptvsd

# # Allow other computers to attach to ptvsd at this IP address and port.
# ptvsd.enable_attach(address=('10.190.0.160', 8097), redirect_output=True)

# # Pause the program until a remote debugger is attached
# ptvsd.wait_for_attach()

model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('data', metavar='DIR',
                    help='path to dataset')
parser.add_argument('-a', '--arch', metavar='ARCH', default='resnet18',
                    choices=model_names,
                    help='model architecture: ' +
                        ' | '.join(model_names) +
                        ' (default: resnet18)')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')

parser.add_argument('-b', '--batch-size', default=256, type=int,
                    metavar='N',
                    help='mini-batch size (default: 256), this is the total '
                         'batch size of all GPUs on the current node when '
                         'using Data Parallel or Distributed Data Parallel')
parser.add_argument('-p', '--print-freq', default=10, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')
parser.add_argument('--world-size', default=-1, type=int,
                    help='number of nodes for distributed training')
parser.add_argument('--rank', default=-1, type=int,
                    help='node rank for distributed training')
parser.add_argument('--dist-url', default='tcp://224.66.41.62:23456', type=str,
                    help='url used to set up distributed training')
parser.add_argument('--dist-backend', default='nccl', type=str,
                    help='distributed backend')
parser.add_argument('--seed', default=None, type=int,
                    help='seed for initializing training. ')
parser.add_argument('--gpu', default=None, type=int,
                    help='GPU id to use.')
parser.add_argument('--multiprocessing-distributed', action='store_true',
                    help='Use multi-processing distributed training to launch '
                         'N processes per node, which has N GPUs. This is the '
                         'fastest way to use PyTorch for either single node or '
                         'multi node data parallel training')


parser.add_argument('--golden', dest='golden', action='store_true',
                    help='Run golden version')
parser.add_argument('--faulty', dest='faulty', action='store_true',
                    help='Run faulty version')

#####
##  Fault Injection Flags
#####
parser.add_argument('-i', '--injection', dest='injection', action='store_true',
                    help='apply FI model')
parser.add_argument('--layer', default=0, type=int,
                    help='Layer to inject fault.')
parser.add_argument('--bit', default=None, type=int,
                    help='Bit to inject fault. MSB=0 and LSB=31')
parser.add_argument('-feats', '--features', dest='fiFeats', action='store_true',
                    help='inject FI on features/activations')
parser.add_argument('-wts', '--weights', dest='fiWeights', action='store_true',
                    help='inject FI on weights')

parser.add_argument('--scores', dest='scores', action='store_true',
                    help='turn scores loging on')

parser.add_argument('--prefix-output', dest='fidPrefix', default='out', type=str,
                    help='prefix of output filenames')

#####
##  Quantization Flags
#####
parser.add_argument('--quantize', dest='quantize', action='store_true',
                    help='apply quantization to model')
parser.add_argument('--quant-type', dest='quant_type', default='SYMMETRIC', type=str,
                    help='Type of quantization: "sym", "asym_u", "asym_s"')
parser.add_argument('--quant-feats', dest='quant_bfeats', default=8, type=int,
                    help='# of bits to quantize features')
parser.add_argument('--quant-wts', dest='quant_bwts', default=8, type=int,
                    help='# of bits to quantize weights')
parser.add_argument('--quant-accum', dest='quant_baccum', default=32, type=int,
                    help='# of bits of accumulator used during quantization')
parser.add_argument('--quant-clip', dest='quant_clip', action='store_true',
                    help='enable clipping of features during quantization')
parser.add_argument('--quant-channel', dest='quant_channel', action='store_true',
                    help='enable per-channel quantization of weights')

#####
##  Pruning Flags
#####


parser.add_argument('-l', '--log', dest='log', action='store_true',
                    help='turn loging on')


def main():
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    if args.dist_url == "env://" and args.world_size == -1:
        args.world_size = int(os.environ["WORLD_SIZE"])

    args.distributed = args.world_size > 1 or args.multiprocessing_distributed

    if args.gpu is not None:
        ngpus_per_node = torch.cuda.device_count()
        if args.multiprocessing_distributed:
            # Since we have ngpus_per_node processes per node, the total world_size
            # needs to be adjusted accordingly
            args.world_size = ngpus_per_node * args.world_size
            # Use torch.multiprocessing.spawn to launch distributed processes: the
            # main_worker process function
            mp.spawn(main_gpu_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, args))
        else:
            # Simply call main_worker function
            main_gpu_worker(args.gpu, ngpus_per_node, args)
    else:
        main_cpu_worker(args)

def main_gpu_worker(gpu, ngpus_per_node, args):
    args.gpu = gpu

    if args.gpu is not None:
        print("Use GPU: {} for inference".format(args.gpu))

    if args.distributed:
        if args.dist_url == "env://" and args.rank == -1:
            args.rank = int(os.environ["RANK"])
        if args.multiprocessing_distributed:
            # For multiprocessing distributed training, rank needs to be the
            # global rank among all the processes
            args.rank = args.rank * ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)
    # create model
    if args.pretrained:
        print("=> using pre-trained model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=True)
    else:
        print("=> creating model '{}'".format(args.arch))
        model = models.__dict__[args.arch]()

    if args.distributed:
        # For multiprocessing distributed, DistributedDataParallel constructor
        # should always set the single device scope, otherwise,
        # DistributedDataParallel will use all available devices.
        if args.gpu is not None:
            torch.cuda.set_device(args.gpu)
            model.cuda(args.gpu)
            # When using a single GPU per process and per
            # DistributedDataParallel, we need to divide the batch size
            # ourselves based on the total number of GPUs we have
            args.batch_size = int(args.batch_size / ngpus_per_node)
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
        else:
            model.cuda()
            # DistributedDataParallel will divide and allocate batch_size to all
            # available GPUs if device_ids are not set
            model = torch.nn.parallel.DistributedDataParallel(model)
    elif args.gpu is not None:
        torch.cuda.set_device(args.gpu)
        model = model.cuda(args.gpu)
    else:
        # DataParallel will divide and allocate batch_size to all available GPUs
        if args.arch.startswith('alexnet') or args.arch.startswith('vgg'):
            model.features = torch.nn.DataParallel(model.features)
            model.cuda()
        else:
            model = torch.nn.DataParallel(model).cuda()

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda(args.gpu)

    cudnn.benchmark = True

    # Data loading code
    valdir = os.path.join(args.data, 'val')
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    val_loader = torch.utils.data.DataLoader(
        datasets.ImageFolder(valdir, transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            normalize,
        ])),
        batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True)

    if args.evaluate:
        validate(val_loader, model, criterion, args)
        return


def main_cpu_worker(args):
    # create model
    if args.pretrained:
        print("=> using pre-trained model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=True)
    else:
        print("=> creating model '{}'".format(args.arch))
        model = models.__dict__[args.arch]()


    # DataParallel will divide and allocate batch_size to all available CPUs
    if args.arch.startswith('alexnet') or args.arch.startswith('vgg'):
        model.features = torch.nn.DataParallel(model.features)
        model.cpu()
    else:
        # Model are saved into self.module when using DataParellel with CPU only
        model = torch.nn.DataParallel(model).module

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss()

    cudnn.benchmark = True

    # Data loading code
    valdir = os.path.join(args.data, 'val')
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    val_loader = torch.utils.data.DataLoader(
        datasets.ImageFolder(valdir, transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            normalize,
        ])),
        batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=False)

    if args.evaluate:
        validate(val_loader, model, criterion, args)
        return


def validate(val_loader, model, criterion, args):
    # loging configs to screen
    logConfig("model", "{}".format(args.arch))
    logConfig("quantization", "{}".format(args.quantize))
    if args.quantize:
        logConfig("mode", "{}".format(args.quant_type))
        logConfig("# bits features", "{}".format(args.quant_bfeats))
        logConfig("# bits weights", "{}".format(args.quant_bwts))
        logConfig("# bits accumulator", "{}".format(args.quant_baccum))
        logConfig("clip", "{}".format(args.quant_clip))
        logConfig("per-channel", "{}".format(args.quant_channel))
    logConfig("injection", "{}".format(args.injection))
    if args.injection:
        logConfig("layer", "{}".format(args.layer))
        logConfig("bit", "{}".format(args.bit))
        logConfig("location:", "  ")
        logConfig("\t features ", "{}".format(args.fiFeats))
        logConfig("\t weights ", "{}".format(args.fiWeights))
        if not(args.fiFeats ^ args.fiWeights): 
            logConfig(" ", "Setting random mode.")
    logConfig("batch size", "{}".format(args.batch_size))
    
    # switch to evaluate mode
    model.eval()

    # applying faulty injection scheme
    fi = FI(model, fiMode=args.injection, fiLayer=args.layer, fiBit=args.bit, fiFeatures=args.fiFeats, fiWeights=args.fiWeights,
            quantMode=args.quantize, quantType=args.quant_type, quantBitFeats=args.quant_bfeats, quantBitWts=args.quant_bwts, quantBitAccum=args.quant_baccum,
            quantClip=args.quant_clip, quantChannel=args.quant_channel, log=args.log)

    fi.traverseModel(model)
    print(model._modules.items())

    batch_time = AverageMeter()
    sdcs = SDCMeter()

    # Golden Run
    if args.golden:
        top1_golden = AverageMeter()
        top5_golden = AverageMeter()

        fi.injectionMode = False
        
        with torch.no_grad():
            end = time.time()
            for i, (input, target) in enumerate(val_loader):
                if args.gpu is not None:
                    input = input.cuda(args.gpu, non_blocking=True)
                    target = target.cuda(args.gpu, non_blocking=True)
                
                # compute output
                output = model(input)

                # measure accuracy
                acc1, acc5 = accuracy(output, target, topk=(1, 5))
                top1_golden.update(acc1[0], input.size(0))
                top5_golden.update(acc5[0], input.size(0))

                sdcs.updateGoldenData(output)

                scores, predictions = topN(output, target, topk=(1,5))
                sdcs.updateGoldenBatchPred(predictions)
                sdcs.updateGoldenBatchScore(scores)

                # measure elapsed time
                batch_time.update(time.time() - end)
                end = time.time()

                if i % args.print_freq == 0:
                    print('Golden Run: [{0}/{1}]\t'
                        'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                        'Acc@1 {top1_golden.val:.3f} ({top1_golden.avg:.3f})\t'
                        'Acc@5 {top5_golden.val:.3f} ({top5_golden.avg:.3f})'.format(
                        i, len(val_loader), batch_time=batch_time, top1_golden=top1_golden, top5_golden=top5_golden))
                # break

    batch_time.reset()

    # Faulty Run
    if args.faulty:
        top1_faulty = AverageMeter()
        top5_faulty = AverageMeter()
        
        fi.injectionMode = True

        with torch.no_grad():
            end = time.time()

            for i, (input, target) in enumerate(val_loader):
                if args.gpu is not None:
                    input = input.cuda(args.gpu, non_blocking=True)
                    target = target.cuda(args.gpu, non_blocking=True)

                # compute output
                output = model(input)
                
                # measure accuracy
                acc1, acc5 = accuracy(output, target, topk=(1, 5))
                top1_faulty.update(acc1[0], input.size(0))
                top5_faulty.update(acc5[0], input.size(0))

                sdcs.updateFaultyData(output)

                scores, predictions = topN(output, target, topk=(1,5))
                sdcs.updateFaultyBatchPred(predictions)
                sdcs.updateFaultyBatchScore(scores)

                # measure elapsed time
                batch_time.update(time.time() - end)
                end = time.time()

                if i % args.print_freq == 0:
                    print('Faulty Run: [{0}/{1}]\t'
                        'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                        'Acc@1 {top1_faulty.val:.3f} ({top1_faulty.avg:.3f})\t'
                        'Acc@5 {top5_faulty.val:.3f} ({top5_faulty.avg:.3f})'.format(
                        i, len(val_loader), batch_time=batch_time, top1_faulty=top1_faulty, top5_faulty=top5_faulty))
                # break
            
    if args.golden:
        print('Golden Run * Acc@1 {top1_golden.avg:.3f} Acc@5 {top5_golden.avg:.3f}'
            .format(top1_golden=top1_golden, top5_golden=top5_golden))

    if args.faulty:
        print('Faulty Run * Acc@1 {top1_faulty.avg:.3f} Acc@5 {top5_faulty.avg:.3f}'
            .format(top1_faulty=top1_faulty, top5_faulty=top5_faulty))

    if args.golden and args.faulty:
        print('Acc@1 {top1_diff:.3f} Acc@5 {top5_diff:.3f}'
            .format(top1_diff=(top1_golden.avg - top1_faulty.avg), top5_diff=(top5_golden.avg - top5_faulty.avg)))        
        sdcs.calculteSDCs()
        print('SDCs * SDC@1 {sdc.top1SDC:.3f} SDC@5 {sdc.top5SDC:.3f}'
            .format(sdc=sdcs))

    if args.scores:
        sdcs.writeScoresNPZData(args.fidPrefix, args.golden, args.faulty)
        # sdcs.writeScores(args.fidPrefix, args.golden, args.faulty)

    # writeOutData(args.fidPrefix, [top1_golden.avg, top5_golden.avg], [top1_faulty.avg, top5_faulty.avg], [sdcs.top1SDC, sdcs.top5SDC])

    return


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
        self.avg = self.sum / float(self.count)


class SDCMeter(object):
    """Stores the SDCs probabilities"""
    def __init__(self):
        self.reset()

    def updateAcc(self, acc1, acc5):
        self.acc1 = acc1
        self.acc5 = acc5

    def updateGoldenData(self, scoreTensors):
        for scores in scoreTensors.cpu().numpy():
            self.goldenScoresAll.append(scores)

    def updateFaultyData(self, scoreTensors):
        for scores in scoreTensors.cpu().numpy():
            self.faultyScoresAll.append(scores)
    
    def updateGoldenBatchPred(self, predTensors):
        self.goldenPred.append(predTensors)

    def updateFaultyBatchPred(self, predTensors):
        self.faultyPred.append(predTensors)

    def updateGoldenBatchScore(self, scoreTensors):
        self.goldenScores.append(scoreTensors)

    def updateFaultyBatchScore(self, scoreTensors):
        self.faultyScores.append(scoreTensors)

    def calculteSDCs(self):
        top1Sum = 0
        top5Sum = 0
        for goldenTensor, faultyTensor in zip(self.goldenPred, self.faultyPred):
            correct = goldenTensor.ne(faultyTensor)
            top1Sum += correct[:1].view(-1).int().sum(0, keepdim=True)
            for goldenRow, faultyRow in zip(goldenTensor.t(), faultyTensor.t()):
                if goldenRow[0] not in faultyRow:
                    top5Sum += 1
        # calculate top1 and top5 SDCs by dividing sum to numBatches * batchSize
        self.top1SDC = float(top1Sum[0]) / float(len(self.goldenPred) * len(self.goldenPred[0][0]))
        self.top5SDC = float(top5Sum) / float(len(self.goldenPred) * len(self.goldenPred[0][0]))
        self.top1SDC *= 100
        self.top5SDC *= 100

    def writeScores(self, fidPrefixName, golden, faulty):
        def writeFID(fidScore, scores):
            with open(fidScore, 'w') as fscore:
                for scoreTensor in scores:
                    for row in scoreTensor:
                        for val in row:
                            fscore.write("%2.4f " % val)
                        fscore.write("\n")
        if golden:
            fidGolden = fidPrefixName + '_score_golden.txt'
            writeFID(fidGolden, self.goldenScores)
        if faulty:
            fidFaulty = fidPrefixName + '_score_faulty.txt'
            writeFID(fidFaulty, self.faultyScores)

    def writeScoresNPZData(self, fidPrefixName, golden, faulty):
        def writeFID(fidScore, scores):
            np.savez_compressed(fidScore, *np.vstack(scores))
        if golden:
            fidGolden = fidPrefixName + '_score_golden.npz'
            writeFID(fidGolden, self.goldenScoresAll)
        if faulty:
            fidFaulty = fidPrefixName + '_score_faulty.npz'
            writeFID(fidFaulty, self.faultyScoresAll)

    def reset(self):
        self.acc1 = 0
        self.acc5 = 0
        self.top1SDC = 0.0
        self.top5SDC = 0.0
        self.goldenPred = []
        self.faultyPred = []
        self.goldenScores = []
        self.faultyScores = []
        self.goldenScoresAll = []
        self.faultyScoresAll = []


def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        acc, pred = output.topk(maxk, 1, True, True)
        # t() == transpose tensor
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


def topN(output, target, topk=(1,)):
    """Return label prediction from top 5 classes"""
    with torch.no_grad():
        maxk = max(topk)
        scores, pred = output.topk(maxk, 1, True, True)
    return scores.t(), pred.t()


def writeOutData(fidPrefixName, accGolden, accFaulty, sdcs):
    cwd = os.getcwd()
    fid = cwd + '/' + fidPrefixName + '_out.npz'
    np.savez_compressed(fid, accGolden=np.array(accGolden, dtype=np.float32), accFaulty=np.array(accFaulty, 
                        dtype=np.float32), sdcs=np.array(sdcs, dtype=np.float32))


if __name__ == '__main__':
    main()