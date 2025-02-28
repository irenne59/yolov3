import argparse
import time

import torch.distributed as dist
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
from torch.utils.data import DataLoader

import test  # import test.py to get mAP after each epoch
from models import *
from utils.datasets import *
from utils.utils import *

#      0.149      0.241      0.126      0.156       6.85      1.008      1.421    0.07989      16.94      6.215      10.61      4.272      0.251      0.001         -4        0.9     0.0005   320 64-1 giou
hyp = {'giou': 1.008,  # giou loss gain
       'xy': 1.421,  # xy loss gain
       'wh': 0.07989,  # wh loss gain
       'cls': 16.94,  # cls loss gain
       'cls_pw': 6.215,  # cls BCELoss positive_weight
       'conf': 10.61,  # conf loss gain
       'conf_pw': 4.272,  # conf BCELoss positive_weight
       'iou_t': 0.251,  # iou target-anchor training threshold
       'lr0': 0.001,  # initial learning rate
       'lrf': -4.,  # final learning rate = lr0 * (10 ** lrf)
       'momentum': 0.90,  # SGD momentum
       'weight_decay': 0.0005}  # optimizer weight decay


#     0.0945      0.279      0.114      0.131         25      0.035        0.2        0.1      0.035         79       1.61       3.53       0.29      0.001         -4        0.9     0.0005   320 64-1
#     0.112       0.265      0.111      0.144       12.6      0.035        0.2        0.1      0.035         79       1.61       3.53       0.29      0.001         -4        0.9     0.0005   320 32-2
# hyp = {'giou': .035,  # giou loss gain
#        'xy': 0.20,  # xy loss gain
#        'wh': 0.10,  # wh loss gain
#        'cls': 0.035,  # cls loss gain
#        'cls_pw': 79.0,  # cls BCELoss positive_weight
#        'conf': 1.61,  # conf loss gain
#        'conf_pw': 3.53,  # conf BCELoss positive_weight
#        'iou_t': 0.29,  # iou target-anchor training threshold
#        'lr0': 0.001,  # initial learning rate
#        'lrf': -4.,  # final learning rate = lr0 * (10 ** lrf)
#        'momentum': 0.90,  # SGD momentum
#        'weight_decay': 0.0005}  # optimizer weight decay


def train(
        cfg,
        data_cfg,
        img_size=416,
        epochs=100,  # 500200 batches at bs 16, 117263 images = 273 epochs
        batch_size=8,
        accumulate=8,  # effective bs = batch_size * accumulate = 8 * 8 = 64
        freeze_backbone=False,
        outdir='/home/eiy_research_59/output/fine-tuning-out',
        pretrained_weight='weights/yolov3.pt'
):
    print('== Start training ==')
    init_seeds()

    weights =  'weights' + os.sep
    
    #------------------------------------
    # get current timestamp
    #------------------------------------
    ts = time.gmtime()
    currentDateTime = time.strftime("%Y%m%d_%H%M%S", ts)
    outdir = outdir + '_' + currentDateTime

    print('Creating directory ' + outdir)
    os.mkdir(outdir)

    latest = outdir  + os.sep + 'latest.pt'
    best = outdir  +  os.sep + 'best.pt'
    device = torch_utils.select_device()
    img_size_test = img_size  # image size for testing
    multi_scale = not opt.single_scale

    print('== Check if it is a multiscale training ==')
    if multi_scale:
        print('== ')
        img_size_min = round(img_size / 32 / 1.5)
        img_size_max = round(img_size / 32 * 1.5)
        img_size = img_size_max * 32  # initiate with maximum multi_scale size

    # Configure run
    print ('== Configure run ==')
    data_dict = parse_data_cfg(data_cfg)
    train_path = data_dict['train']
    nc = int(data_dict['classes'])  # number of classes

    # Initialize model
    print('== Initialize model ==')
    model = Darknet(cfg).to(device)

    # Optimizer
    print('== Optimizer ==')
    optimizer = optim.SGD(model.parameters(), lr=hyp['lr0'], momentum=hyp['momentum'], weight_decay=hyp['weight_decay'])

    cutoff = -1  # backbone reaches to cutoff layer
    start_epoch = 0
    best_loss = float('inf')
    nf = int(model.module_defs[model.yolo_layers[0] - 1]['filters'])  # yolo layer size (i.e. 255)
    if opt.resume or opt.transfer:  # Load previously saved model
        if opt.transfer:  # Transfer learning
            print('== Load transfer learning model ==')
            chkpt = torch.load(pretrained_weight, map_location=device)
            model.load_state_dict({k: v for k, v in chkpt['model'].items() if v.numel() > 1 and v.shape[0] != 255},
                                  strict=False)
            for p in model.parameters():
                p.requires_grad = True if p.shape[0] == nf else False

        else:  # resume from latest.pt
            print ('== Resume from latest model ==')
            chkpt = torch.load(pretrained_weight, map_location=device)  # load checkpoint
            model.load_state_dict(chkpt['model'])

        start_epoch = chkpt['epoch'] + 1
        if chkpt['optimizer'] is not None:
            optimizer.load_state_dict(chkpt['optimizer'])
            best_loss = chkpt['best_loss']
        del chkpt

    else:  # Initialize model with backbone (optional)
        print('== Training from scratch ==')
        if '-tiny.cfg' in cfg:
            cutoff = load_darknet_weights(model, weights + 'yolov3-tiny.conv.15')
        else:
            cutoff = load_darknet_weights(model, weights + 'darknet53.conv.74')

        # Remove old results
        print('== Remove old results ==')
        for f in glob.glob(outdir + '/*_batch*.jpg') + glob.glob(outdir + '/results.txt'):
            os.remove(f)

    # Scheduler https://github.com/ultralytics/yolov3/issues/238
    # lf = lambda x: 1 - x / epochs  # linear ramp to zero
    # lf = lambda x: 10 ** (hyp['lrf'] * x / epochs)  # exp ramp
    # lf = lambda x: 1 - 10 ** (hyp['lrf'] * (1 - x / epochs))  # inverse exp ramp
    # scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=lf)
    print('== Scheduler ==')
    scheduler = lr_scheduler.MultiStepLR(optimizer, milestones=[round(opt.epochs * x) for x in (0.8, 0.9)], gamma=0.1)
    scheduler.last_epoch = start_epoch - 1

    # # Plot lr schedule
    # y = []
    # for _ in range(epochs):
    #     scheduler.step()
    #     y.append(optimizer.param_groups[0]['lr'])
    # plt.plot(y, label='LambdaLR')
    # plt.xlabel('epoch')
    # plt.ylabel('LR')
    # plt.tight_layout()
    # plt.savefig('LR.png', dpi=300)

    # Dataset
    print('== Load Dataset ==')
    rectangular_training = False
    dataset = LoadImagesAndLabels(train_path,
                                  img_size,
                                  batch_size,
                                  augment=True,
                                  rect=rectangular_training)

    # Initialize distributed training
    if torch.cuda.device_count() > 1:
        print('== Initialize distributed training ==')
        dist.init_process_group(backend='nccl',  # 'distributed backend'
                                init_method='tcp://127.0.0.1:9999',  # distributed training init method
                                world_size=1,  # number of nodes for distributed training
                                rank=0)  # distributed training node rank

        model = torch.nn.parallel.DistributedDataParallel(model)
        # sampler = torch.utils.data.distributed.DistributedSampler(dataset)

    # Dataloader
    print('== Data Loader ==')
    dataloader = DataLoader(dataset,
                            batch_size=batch_size,
                            num_workers=opt.num_workers,
                            shuffle=not rectangular_training,  # Shuffle=True unless rectangular training is used
                            pin_memory=True,
                            collate_fn=dataset.collate_fn)

    # Mixed precision training https://github.com/NVIDIA/apex
    mixed_precision = True
    if mixed_precision:
        print('== Mixed precision training ==')
        try:
            from apex import amp
            model, optimizer = amp.initialize(model, optimizer, opt_level='O1')
        except:  # not installed: install help: https://github.com/NVIDIA/apex/issues/259
            mixed_precision = False

    # Start training
    print('== Attach hyperparameters to model ==')
    model.hyp = hyp  # attach hyperparameters to model
    # model.class_weights = labels_to_class_weights(dataset.labels, nc).to(device)  # attach class weights
    print('== Model summary ==')
    model_info(model, report='summary')  # 'full' or 'summary'

    print('== Start training ==')
    nb = len(dataloader)
    maps = np.zeros(nc)  # mAP per class
    results = (0, 0, 0, 0, 0)  # P, R, mAP, F1, test_loss
    n_burnin = min(round(nb / 5 + 1), 1000)  # burn-in batches
    t, t0 = time.time(), time.time()

    resultFile = open(outdir + '/results.txt', 'w')

    for epoch in range(start_epoch, epochs):
        model.train()
        print(('\n%8s%12s' + '%10s' * 7) %
              ('Epoch', 'Batch', 'xy', 'wh', 'conf', 'cls', 'total', 'targets', 'img_size'))

        # Update scheduler
        scheduler.step()

        # Freeze backbone at epoch 0, unfreeze at epoch 1 (optional)
        if freeze_backbone and epoch < 2:
            for name, p in model.named_parameters():
                if int(name.split('.')[1]) < cutoff:  # if layer < 75
                    p.requires_grad = False if epoch == 0 else True

        # # Update image weights (optional)
        # w = model.class_weights.cpu().numpy() * (1 - maps)  # class weights
        # image_weights = labels_to_image_weights(dataset.labels, nc=nc, class_weights=w)
        # dataset.indices = random.choices(range(dataset.n), weights=image_weights, k=dataset.n)  # random weighted index

        mloss = torch.zeros(5).to(device)  # mean losses
        pbar = tqdm(enumerate(dataloader), total=nb)  # progress bar
        for i, (imgs, targets, _, _) in pbar:
            imgs = imgs.to(device)
            targets = targets.to(device)

            # Multi-Scale training
            if multi_scale:
                if (i + nb * epoch) / accumulate % 10 == 0:  #  adjust (67% - 150%) every 10 batches
                    img_size = random.choice(range(img_size_min, img_size_max + 1)) * 32
                    # print('img_size = %g' % img_size)
                scale_factor = img_size / max(imgs.shape[-2:])
                imgs = F.interpolate(imgs, scale_factor=scale_factor, mode='bilinear', align_corners=False)

            # Plot images with bounding boxes
            if epoch == 0 and i == 0:
                plot_images(imgs=imgs, targets=targets, fname='train_batch%g.jpg' % i)

            # SGD burn-in
            if epoch == 0 and i <= n_burnin:
                lr = hyp['lr0'] * (i / n_burnin) ** 4
                for x in optimizer.param_groups:
                    x['lr'] = lr

            # Run model
            pred = model(imgs)

            # Compute loss
            loss, loss_items = compute_loss(pred, targets, model, giou_loss=opt.giou)
            if torch.isnan(loss):
                print('WARNING: nan loss detected, ending training')
                return results

            # Compute gradient
            if mixed_precision:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            # Accumulate gradient for x batches before optimizing
            if (i + 1) % accumulate == 0 or (i + 1) == nb:
                optimizer.step()
                optimizer.zero_grad()

            # Print batch results
            mloss = (mloss * i + loss_items) / (i + 1)  # update mean losses
            # s = ('%8s%12s' + '%10.3g' * 7) % ('%g/%g' % (epoch, epochs - 1), '%g/%g' % (i, nb - 1), *mloss, len(targets), time.time() - t)
            s = ('%8s%12s' + '%10.3g' * 7) % (
                '%g/%g' % (epoch, epochs - 1), '%g/%g' % (i, nb - 1), *mloss, len(targets), img_size)
            t = time.time()
            pbar.set_description(s)  # print(s)

        # Report time
        print('== Report time ==')
        dt = (time.time() - t0) / 3600
        print('%g epochs completed in %.3f hours.' % (epoch - start_epoch + 1, dt))

        # Calculate mAP (always test final epoch, skip first 5 if opt.nosave)
        if not (opt.notest or (opt.nosave and epoch < 10)) or epoch == epochs - 1:
            print('== Calculate mAP ==')
            with torch.no_grad():
                results, maps = test.test(cfg, data_cfg, batch_size=batch_size, img_size=img_size_test, model=model,
                                          conf_thres=0.5)

        # Write epoch results
        print('== Write epoch results ==')
        with open(outdir + '/results.txt', 'a') as file:
            file.write(s + '%11.3g' * 5 % results + '\n')  # P, R, mAP, F1, test_loss

        # Update best loss
        print('== Update best loss ==')
        test_loss = results[4]
        if test_loss < best_loss:
            best_loss = test_loss

        # Save training results
        save = (not opt.nosave) or (epoch == epochs - 1)
        if save:
            print('== Save training results ==')
            # Create checkpoint
            chkpt = {'epoch': epoch,
                     'best_loss': best_loss,
                     'model': model.module.state_dict() if type(
                         model) is nn.parallel.DistributedDataParallel else model.state_dict(),
                     'optimizer': optimizer.state_dict()}

            # Save latest checkpoint
            torch.save(chkpt, latest)

            # Save best checkpoint
            if best_loss == test_loss:
                torch.save(chkpt, best)

            # Save backup every 10 epochs (optional)
            if epoch > 0 and epoch % 10 == 0:
                torch.save(chkpt, outdir + '/backup%g.pt' % epoch)

            # Delete checkpoint
            del chkpt

    return results


def print_mutation(hyp, results):
    print('Write mutation results')
    # Write mutation results
    a = '%11s' * len(hyp) % tuple(hyp.keys())  # hyperparam keys
    b = '%11.4g' * len(hyp) % tuple(hyp.values())  # hyperparam values
    c = '%11.3g' * len(results) % results  # results (P, R, mAP, F1, test_loss)
    print('\n%s\n%s\nEvolved fitness: %s\n' % (a, b, c))

    if opt.cloud_evolve:
        os.system('gsutil cp gs://yolov4/evolve.txt .')  # download evolve.txt
        with open('evolve.txt', 'a') as f:  # append result to evolve.txt
            f.write(c + b + '\n')
        os.system('gsutil cp evolve.txt gs://yolov4')  # upload evolve.txt
    else:
        with open('evolve.txt', 'a') as f:
            f.write(c + b + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=100, help='number of epochs')
    parser.add_argument('--batchsize', type=int, default=16, help='batch size')
    parser.add_argument('--accumulate', type=int, default=8, help='number of batches to accumulate before optimizing')
    parser.add_argument('--cfg', type=str, default='/home/eiy_research_59/dbmodel221/yolov3.cfg', help='cfg file path')
    parser.add_argument('--data', type=str, default='/home/eiy_research_59/dbmodel221/dbmodel221.data', help='coco.data file path')
    parser.add_argument('--single-scale', action='store_true', help='train at fixed size (no multi-scale)')
    parser.add_argument('--imgsize', type=int, default=416, help='inference size (pixels)')
    parser.add_argument('--resume', action='store_true', help='resume training flag')
    parser.add_argument('--transfer', action='store_true', help='transfer learning flag')
    parser.add_argument('--num-workers', type=int, default=4, help='number of Pytorch DataLoader workers')
    parser.add_argument('--nosave', action='store_true', help='only save final checkpoint')
    parser.add_argument('--notest', action='store_true', help='only test final epoch')
    parser.add_argument('--giou', action='store_true', help='use GIoU loss instead of xy, wh loss')
    parser.add_argument('--evolve', action='store_true', help='evolve hyperparameters')
    parser.add_argument('--cloud-evolve', action='store_true', help='evolve hyperparameters from a cloud source')
    parser.add_argument('--var', default=0, type=int, help='debug variable')
    parser.add_argument('--outdir', default='fine-tuning-out', type=str, help='output directory')
    parser.add_argument('--pretrainedweight', default='weights/yolov3.pt', type=str, help='pretrained weight dir')
    opt = parser.parse_args()
    print(opt)

    opt.evolve = opt.cloud_evolve or opt.evolve
    if opt.evolve:
        opt.notest = True  # only test final epoch
        opt.nosave = True  # only save final checkpoint

    # Train
    results = train(opt.cfg,
                    opt.data,
                    img_size=opt.imgsize,
                    epochs=opt.epochs,
                    batch_size=opt.batchsize,
                    accumulate=opt.accumulate,
                    outdir = opt.outdir, 
                    pretrained_weight = opt.pretrainedweight)

    #plot_results(opt.outdir, 0, opt.epochs)
    # Evolve hyperparameters (optional)
    if opt.evolve:
        gen = 1000  # generations to evolve
        print_mutation(hyp, results)  # Write mutation results

        for _ in range(gen):
            # Get best hyperparamters
            x = np.loadtxt('evolve.txt', ndmin=2)
            x = x[x[:, 2].argmax()]  # select best mAP as genetic fitness (col 2)
            for i, k in enumerate(hyp.keys()):
                hyp[k] = x[i + 5]

            # Mutate
            init_seeds(seed=int(time.time()))
            s = [.2, .2, .2, .2, .2, .2, .2, .2, .2 * 0, .2 * 0, .05 * 0, .2 * 0]  # fractional sigmas
            for i, k in enumerate(hyp.keys()):
                x = (np.random.randn(1) * s[i] + 1) ** 2.0  # plt.hist(x.ravel(), 300)
                hyp[k] *= float(x)  # vary by 20% 1sigma

            # Clip to limits
            keys = ['lr0', 'iou_t', 'momentum', 'weight_decay']
            limits = [(1e-4, 1e-2), (0, 0.70), (0.70, 0.98), (0, 0.01)]
            for k, v in zip(keys, limits):
                hyp[k] = np.clip(hyp[k], v[0], v[1])

            # Train mutation
            results = train(opt.cfg,
                            opt.data_cfg,
                            img_size=opt.img_size,
                            epochs=opt.epochs,
                            batch_size=opt.batch_size,
                            accumulate=opt.accumulate)

            # Write mutation results
            print_mutation(hyp, results)

            # # Plot results
            # import numpy as np
            # import matplotlib.pyplot as plt
            # a = np.loadtxt('evolve_1000val.txt')
            # x = a[:, 2] * a[:, 3]  # metric = mAP * F1
            # weights = (x - x.min()) ** 2
            # fig = plt.figure(figsize=(14, 7))
            # for i in range(len(hyp)):
            #     y = a[:, i + 5]
            #     mu = (y * weights).sum() / weights.sum()
            #     plt.subplot(2, 5, i+1)
            #     plt.plot(x.max(), mu, 'o')
            #     plt.plot(x, y, '.')
            #     print(list(hyp.keys())[i],'%.4g' % mu)
