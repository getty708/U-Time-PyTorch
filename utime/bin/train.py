"""
Script for training a model on a set of data
Should be called form within a U-Time project directory, call 'ut init' first.
All hyperparameters should be stored in the hparams.yaml file (generated by
ut init).
"""

from argparse import ArgumentParser
import os


def get_argparser():
    """
    Returns an argument parser for this script
    """
    parser = ArgumentParser(description='Fit a U-Time model defined in'
                                        ' a project folder. Invoke '
                                        '"ut init" to start a new project.')
    parser.add_argument("--num_GPUs", type=int, default=1,
                        help="Number of GPUs to use for this job (default=1)")
    parser.add_argument("--force_GPU", type=str, default="")
    parser.add_argument("--continue_training", action="store_true",
                        help="Continue the last training session")
    parser.add_argument("--initialize_from", type=str, default=None,
                        help="Path to a model weights file to initialize from.")
    parser.add_argument("--log_file_prefix", type=str,
                        help="Optional prefix for logfiles.", default="")
    parser.add_argument("--overwrite", action='store_true',
                        help='overwrite previous training session in the '
                             'project path')
    parser.add_argument("--just_one", action="store_true",
                        help="For testing purposes, run only on the first "
                             "training and validation samples.")
    parser.add_argument("--no_val", action="store_true",
                        help="For testing purposes, do not perform validation.")
    parser.add_argument("--max_train_samples_per_epoch", type=int,
                        default=5e5,
                        help="Maximum number of sleep stages to sample in each"
                             "epoch. (defaults to 5e5)")
    parser.add_argument("--val_samples_per_epoch", type=int,
                        default=5e4,
                        help="Number of sleep stages to sample in each"
                             "round of validation. (defaults to 5e4)")
    parser.add_argument("--n_epochs", type=int, default=None,
                        help="Overwrite the number of epochs specified in the"
                             " hyperparameter file with this number (int).")
    parser.add_argument("--channels", nargs='*', type=str, default=None,
                        help="A list of channels to use instead of those "
                             "specified in the parameter file.")
    parser.add_argument("--final_weights_file_name", type=str,
                        default="model_weights.h5")
    parser.add_argument("--train_on_val", action="store_true",
                        help="Include the validation set in the training set."
                             " Will force --no_val to be active.")
    return parser


def assert_args(args):
    """ Implements a limited set of checks on the passed arguments """
    if args.continue_training and args.initialize_from:
        raise ValueError("Should not specify both --continue_training and "
                         "--initialize_from")
    if args.max_train_samples_per_epoch < 1 or args.val_samples_per_epoch < 1:
        raise ValueError("max_train_samples_per_epoch and "
                         "val_samples_per_epoch must be >= 1.")
    if args.n_epochs is not None and args.n_epochs < 1:
        raise ValueError("n_epochs must be larger than >= 1.")


def update_hparams_with_command_line_arguments(hparams, args):
    """
    Overwrite hyperparameters stored in YAMLHparams object 'hparams' according
    to passed args.

    Args:
        hparams: (YAMLHparams) The hyperparameter object to write parameters to
        args:    (Namespace)   Passed command-line arguments
        logger:  (Logger)      A Logger instance
    """
    if isinstance(args.n_epochs, int) and args.n_epochs > 0:
        hparams.set_value(subdir="fit",
                          name="n_epochs",
                          value=args.n_epochs,
                          overwrite=True)
        hparams["fit"]["n_epochs"] = args.n_epochs
    if args.channels is not None and args.channels:
        # Channel selection hyperparameter might be stored in separate conf.
        # files. Here, we load them, set the channel value, and save them again
        from utime.utils.scriptutils import get_all_dataset_hparams
        for _, dataset_hparams in get_all_dataset_hparams(hparams).items():
            dataset_hparams.set_value(subdir=None,
                                      name="select_channels",
                                      value=args.channels,
                                      overwrite=True)
            dataset_hparams.save_current()
    hparams.save_current()


def run(args, gpu_mon):
    """
    Run the script according to args - Please refer to the argparser.

    args:
        args:    (Namespace)  command-line arguments
        gpu_mon: (GPUMonitor) Initialized MultiPlanarUNet GPUMonitor object
    """
    assert_args(args)
    from mpunet.logging import Logger
    from utime.train import Trainer
    from utime.hyperparameters import YAMLHParams
    from utime.utils.scriptutils import (assert_project_folder,
                                         make_multi_gpu_model)
    from utime.utils.scriptutils.train import (get_train_and_val_datasets,
                                               get_generators,
                                               find_and_set_gpus,
                                               get_samples_per_epoch,
                                               save_final_weights)

    project_dir = os.path.abspath("./")
    assert_project_folder(project_dir)
    if args.overwrite and not args.continue_training:
        from mpunet.bin.train import remove_previous_session
        remove_previous_session(project_dir)

    # Get logger object
    logger = Logger(project_dir,
                    overwrite_existing=args.overwrite or args.continue_training,
                    log_prefix=args.log_file_prefix)
    logger("Args dump: {}".format(vars(args)))

    # Load hparams
    hparams = YAMLHParams(os.path.join(project_dir, "hparams.yaml"), logger=logger)
    update_hparams_with_command_line_arguments(hparams, args)

    # Initialize and load (potentially multiple) datasets
    datasets, no_val = get_train_and_val_datasets(hparams, args.no_val,
                                                  args.train_on_val, logger)

    # Load data in all datasets
    for data in datasets:
        for d in data:
            d.load(1 if args.just_one else None)
            d.pairs = d.loaded_pairs   # remove the other pairs

    # Get sequence generators for all datasets
    train_seq, val_seq = get_generators(datasets, hparams, no_val)

    # Add additional (inferred) parameters to parameter file
    hparams.set_value("build", "n_classes", train_seq.n_classes, overwrite=True)
    hparams.set_value("build", "batch_shape", train_seq.batch_shape, overwrite=True)
    hparams.save_current()

    if args.continue_training:
        # Prepare the project directory for continued training.
        # Please refer to the function docstring for details
        from utime.models.model_init import prepare_for_continued_training
        parameter_file = prepare_for_continued_training(hparams=hparams,
                                                        project_dir=project_dir,
                                                        logger=logger)
    else:
        parameter_file = args.initialize_from  # most often is None

    # Set the GPU visibility
    num_GPUs = find_and_set_gpus(gpu_mon, args.force_GPU, args.num_GPUs)
    # Initialize and potential load parameters into the model
    from utime.models.model_init import init_model, load_from_file
    org_model = init_model(hparams["build"], logger)
    if parameter_file:
        load_from_file(org_model, parameter_file, logger, by_name=True)
    model, org_model = make_multi_gpu_model(org_model, num_GPUs)

    # Prepare a trainer object. Takes care of compiling and training.
    trainer = Trainer(model, org_model=org_model, logger=logger)
    trainer.compile_model(n_classes=hparams["build"].get("n_classes"),
                          **hparams["fit"])

    # Fit the model on a number of samples as specified in args
    samples_pr_epoch = get_samples_per_epoch(train_seq,
                                             args.max_train_samples_per_epoch,
                                             args.val_samples_per_epoch)
    _ = trainer.fit(train=train_seq,
                    val=val_seq,
                    train_samples_per_epoch=samples_pr_epoch[0],
                    val_samples_per_epoch=samples_pr_epoch[1],
                    **hparams["fit"])

    # Save weights to project_dir/model/{final_weights_file_name}.h5
    # Note: these weights are rarely used, as a checkpoint callback also saves
    # weights to this directory through training
    save_final_weights(project_dir,
                       model=model,
                       file_name=args.final_weights_file_name,
                       logger=logger)


def entry_func(args=None):
    # Get the script to execute, parse only first input
    parser = get_argparser()
    args = parser.parse_args(args)

    # Here, we wrap the training in a try/except block to ensure that we
    # stop the GPUMonitor process after training, even if an error occurred
    from mpunet.utils.system import GPUMonitor
    gpu_mon = GPUMonitor()
    try:
        run(args=args, gpu_mon=gpu_mon)
    except Exception as e:
        gpu_mon.stop()
        raise e


if __name__ == "__main__":
    entry_func()
