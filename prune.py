import argparse
import traceback
import shutil
import logging
import yaml
import sys
import os
import torch
import numpy as np
import torch.utils.tensorboard as tb
from tqdm import tqdm
from runner.diffusion_ori import Diffusion
from torchvision import transforms
import torchvision
from datasets import get_dataset, data_transform, inverse_data_transform
import torchvision.utils as tvu
import json
torch.set_printoptions(sci_mode=False)


def parse_args_and_config():
    parser = argparse.ArgumentParser(description=globals()["__doc__"])

    parser.add_argument(
        "--config", type=str, required=True, help="Path to the config file."
    )

    parser.add_argument("--seed", type=int, default=2333, help="Random seed")
    parser.add_argument("--taylor_batch_size", type=int, default=128, help="batch size for taylor expansion")
    parser.add_argument(
        "--exp", type=str, default="exp", help="Path for saving running related data."
    )
    parser.add_argument(
        "--doc",
        type=str,
        required=True,
        help="A string for documentation purpose. "
        "Will be the name of the log folder.",
    )
    parser.add_argument(
        "--comment", type=str, default="", help="A string for experiment comment."
    )

    parser.add_argument(
        "--load_pruned_model", type=str, default=None, help="load pruned models"
    )

    parser.add_argument(
        "--save_pruned_model", type=str, default=None, help="load pruned models"
    )
    parser.add_argument("--save_masks", default=False, action="store_true", help="Whether to test the model")

    parser.add_argument(
        "--verbose",
        type=str,
        default="info",
        help="Verbose level: info | debug | warning | critical",
    )
    parser.add_argument("--test", action="store_true", help="Whether to test the model")
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Whether to produce samples from the model",
    )
    parser.add_argument("--fid", action="store_true")
    parser.add_argument("--interpolation", action="store_true")
    parser.add_argument(
        "--resume_training", action="store_true", help="Whether to resume training"
    )
    parser.add_argument(
        "-i",
        "--image_folder",
        type=str,
        default="images",
        help="The folder name of samples",
    )
    parser.add_argument(
        "--ni",
        action="store_true",
        help="No interaction. Suitable for Slurm Job launcher",
    )
    parser.add_argument(
        "--use_ema",
        action="store_true",
        help="No interaction. Suitable for Slurm Job launcher",
    )
    parser.add_argument("--use_pretrained", action="store_true")
    parser.add_argument(
        "--sample_type",
        type=str,
        default="generalized",
        help="sampling approach (generalized or ddpm_noisy)",
    )
    parser.add_argument(
        "--skip_type",
        type=str,
        default="uniform",
        help="skip according to (uniform or quadratic)",
    )

    parser.add_argument(
        "--pruner",
        type=str,
        default="taylor",
        choices=["taylor", "random", "magnitude", "reinit", "first_order_taylor", "second_order_taylor", 'abs_taylor', 'fisher', 'ours', 'bn'],
    )

    parser.add_argument(
        "--restore_from",
        type=str,
        default=None,
        help="Restore from user a checkpoint",
    )
    parser.add_argument(
        "--timesteps", type=int, default=1000, help="number of steps involved"
    )
    parser.add_argument(
        "--eta",
        type=float,
        default=0.0,
        help="eta used to control the variances of sigma",
    )
    parser.add_argument(
        "--thr",
        type=float,
        default=0.01,
        help="eta used to control the variances of sigma",
    )
    parser.add_argument(
        "--pruning_ratio",
        type=float,
        default=0.0,
        help="pruning ratio",
    )
    parser.add_argument("--train_from_scratch", action="store_true", help="Whether to train model from the scratch")
    
    parser.add_argument("--sequence", action="store_true")

    args = parser.parse_args()
    args.log_path = os.path.join(args.exp, "logs", args.doc)

    # parse config file
    with open(os.path.join("configs", args.config), "r") as f:
        config = yaml.safe_load(f)
    new_config = dict2namespace(config)

    # add device
    device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
    logging.info("Using device: {}".format(device))
    new_config.device = device

    # set random seed
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
    torch.backends.cudnn.benchmark = True
    return args, new_config


def dict2namespace(config):
    namespace = argparse.Namespace()
    for key, value in config.items():
        if isinstance(value, dict):
            new_value = dict2namespace(value)
        else:
            new_value = value
        setattr(namespace, key, new_value)
    return namespace


def main():
    args, config = parse_args_and_config()
    
    runner = Diffusion(args, config)
    if args.pruning_ratio > 0 and args.load_pruned_model is None:
        os.makedirs(os.path.join(args.exp, "pruned", args.doc), exist_ok=True)
        save_dir = os.path.join(args.exp, "pruned", args.doc)
        if args.save_masks:
            masks_dir = os.path.join(save_dir, "masks")
            os.makedirs(masks_dir, exist_ok=True)

        if args.save_pruned_model:
            models_dir = os.path.join(save_dir, "models")
            os.makedirs(models_dir, exist_ok=True)

        dataset, _ = get_dataset(args, config)
        print(f"Dataset size: {len(dataset)}")
        train_dataloader = torch.utils.data.DataLoader(
            dataset, batch_size=args.taylor_batch_size, shuffle=True, num_workers=4, drop_last=True
        )
        import torch_pruning as tp
        print("Pruning ...")
        model = runner.model.eval()
        model.to(runner.device)
        example_inputs = {'x': torch.randn(1, 3, config.data.image_size, config.data.image_size).to(runner.device), 't': torch.ones(1).to(runner.device)}
        if args.pruner == 'taylor':
            imp = tp.importance.TaylorImportance()
        elif args.pruner == 'first_order_taylor':
            imp = tp.importance.FullTaylorImportance(order=1)
        elif args.pruner == 'second_order_taylor':
            imp = tp.importance.FullTaylorImportance(order=2)
        elif args.pruner == 'random' or args.pruner == 'reinit':
            imp = tp.importance.RandomImportance()
        elif args.pruner == 'magnitude':
            imp = tp.importance.MagnitudeImportance()
        elif args.pruner == 'abs_taylor':
            imp = tp.importance.AbsTaylorImportance()
        elif args.pruner == 'fisher':
            imp = tp.importance.FisherImportance()
        elif args.pruner == 'ours':
            imp = tp.importance.TaylorImportance()
        elif args.pruner == 'bn':
            imp = tp.importance.BNScaleImportance()
            

        ignored_layers = [model.conv_out]
        channel_groups = {}
        iterative_steps = 1
        if args.pruner == 'bn':
            pruner = tp.pruner.BNScalePruner(
            model,
            example_inputs,
            importance=imp,
            iterative_steps=iterative_steps,
            ch_sparsity=args.pruning_ratio, # remove 50% channels, ResNet18 = {64, 128, 256, 512} => ResNet18_Half = {32, 64, 128, 256}
            ignored_layers=ignored_layers,
        )
        else:
            pruner = tp.pruner.MagnitudePruner(
                model,
                example_inputs,
                importance=imp,
                iterative_steps=iterative_steps,
                channel_groups =channel_groups,
                ch_sparsity=args.pruning_ratio, # remove 50% channels, ResNet18 = {64, 128, 256, 512} => ResNet18_Half = {32, 64, 128, 256}
                ignored_layers=ignored_layers,
                root_module_types=[torch.nn.Conv2d, torch.nn.Linear]
            ) 
        base_macs, base_nparams = tp.utils.count_ops_and_params(model, example_inputs)
        n = config.sampling.batch_size
        noise = torch.randn(
            n,
            config.data.channels,
            config.data.image_size,
            config.data.image_size,
            device=runner.device,
        )
        
        if 'taylor' in args.pruner or 'fisher' in args.pruner or 'ours' in args.pruner:
            x  = next(iter(train_dataloader))
            if isinstance(x, (list, tuple)):
                x = x[0]
            x = x.to(runner.device)
            x = data_transform(config, x)
            x = x.to(runner.device)
            n = x.size(0)
            e = torch.randn_like(x)
            b = runner.betas
            from functions.losses import loss_registry 
            model.zero_grad()
            max_loss = 0
            for step_k in tqdm(range(0, 1000)):
                t = torch.ones(n, dtype=torch.long).to(runner.device) * step_k
                loss = loss_registry[config.model.type](model, x, t, e, b)
                if args.pruner == 'ours':
                    if loss>max_loss:
                        max_loss = loss
                    if loss<max_loss*args.thr:
                        break
                    print(loss, max_loss)
                loss.backward()
        
        print("============ Before Pruning ============")
        # print(model)
        mask = []
        for (g, pruning_idxs, total_channel) in pruner.step(interactive=True):
            # print(pruning_idxs, pruning_idxs.shape, total_channel)
            idxs = pruning_idxs.cpu()
            mask_=torch.zeros(total_channel)
            mask_.scatter_(0,idxs,1)
            mask.extend(list(mask_.numpy()))
            # print(mask_)
            g.prune()
        # print(mask)
        # print(len(mask))
        # item={"model": args.restore_from, "mask": mask}
        mask_save_dir = os.path.join(masks_dir, args.save_pruned_model.split('/')[-1].replace('.pth', ""))
        # save the mask for the pruned model
        np.save(mask_save_dir, np.array(mask))
        if args.pruner == 'reinit':
            def reset_parameters(model):
                for m in model.modules():
                    if hasattr(m, 'reset_parameters'):
                        m.reset_parameters()
            model.apply(reset_parameters)
        
        macs, nparams = tp.utils.count_ops_and_params(model, example_inputs)
        print("============ After Pruning ============")
        # print(model)
        print("#Params: {:.4f} M => {:.4f} M".format(base_nparams/1e6, nparams/1e6))
        print("#MACs: {:.4f} G => {:.4f} G".format(base_macs/1e9, macs/1e9))
        del pruner
        # Save pruned model
        # os.makedirs(os.path.dirname(args.save_pruned_model), exist_ok=True)
        saved_path = os.path.join(models_dir, args.save_pruned_model)
        print("Saving pruned model as {}".format(saved_path))
        torch.save(
            model,
            saved_path
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
