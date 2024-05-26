"""API for the neps package.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Callable, Literal, List

import ConfigSpace as CS

import metahyper
from metahyper import instance_from_map

from .optimizers import BaseOptimizer, SearcherMapping
from .plot.tensorboard_eval import tblogger
from .search_spaces.parameter import Parameter
from .search_spaces.search_space import SearchSpace, pipeline_space_from_configspace
from .status.status import post_run_csv
from .utils.common import get_searcher_data
from .utils.result_utils import get_loss


def _post_evaluation_hook_function(
    _loss_value_on_error: None | float, _ignore_errors: bool
):
    def _post_evaluation_hook(
        config,
        config_id,
        config_working_directory,
        result,
        logger,
        loss_value_on_error=_loss_value_on_error,
        ignore_errors=_ignore_errors,
    ):
        working_directory = Path(config_working_directory, "../../")
        loss = get_loss(result, loss_value_on_error, ignore_errors)

        # 1. Write all configs and losses
        all_configs_losses = Path(working_directory, "all_losses_and_configs.txt")

        def write_loss_and_config(file_handle, loss_, config_id_, config_):
            file_handle.write(f"Loss: {loss_}\n")
            file_handle.write(f"Config ID: {config_id_}\n")
            file_handle.write(f"Config: {config_}\n")
            file_handle.write(79 * "-" + "\n")

        with all_configs_losses.open("a", encoding="utf-8") as f:
            write_loss_and_config(f, loss, config_id, config)

        # no need to handle best loss cases if an error occurred
        if result == "error":
            return

        # The "best" loss exists only in the pareto sense for multi-objective
        is_multi_objective = isinstance(loss, dict)
        if is_multi_objective:
            logger.info(f"Finished evaluating config {config_id}")
            return

        # 2. Write best losses/configs
        best_loss_trajectory_file = Path(working_directory, "best_loss_trajectory.txt")
        best_loss_config_trajectory_file = Path(
            working_directory, "best_loss_with_config_trajectory.txt"
        )

        if not best_loss_trajectory_file.exists():
            is_new_best = result != "error"
        else:
            best_loss_trajectory = best_loss_trajectory_file.read_text(encoding="utf-8")
            best_loss_trajectory = list(best_loss_trajectory.rstrip("\n").split("\n"))
            best_loss = best_loss_trajectory[-1]
            is_new_best = float(best_loss) > loss

        if is_new_best:
            with best_loss_trajectory_file.open("a", encoding="utf-8") as f:
                f.write(f"{loss}\n")

            with best_loss_config_trajectory_file.open("a", encoding="utf-8") as f:
                write_loss_and_config(f, loss, config_id, config)

            logger.info(
                f"Finished evaluating config {config_id}"
                f" -- new best with loss {float(loss) :.3f}"
            )

        else:
            logger.info(f"Finished evaluating config {config_id}")

        tblogger.end_of_config()

    return _post_evaluation_hook


def run(
    run_pipeline: Callable,
    pipeline_space: dict[str, Parameter | CS.ConfigurationSpace] | CS.ConfigurationSpace,
    root_directory: str | Path,
    overwrite_working_directory: bool = False,
    post_run_summary: bool = False,
    development_stage_id=None,
    task_id=None,
    max_evaluations_total: int | None = None,
    max_evaluations_per_run: int | None = None,
    continue_until_max_evaluation_completed: bool = False,
    max_cost_total: int | float | None = None,
    ignore_errors: bool = False,
    loss_value_on_error: None | float = None,
    cost_value_on_error: None | float = None,
    pre_load_hooks: List=[],
    searcher: Literal[
        "default",
        "bayesian_optimization",
        "random_search",
        "hyperband",
        "priorband",
        "mobster",
        "asha",
        "regularized_evolution",
    ]
    | BaseOptimizer = "default",
    searcher_path: Path | str | None = None,
    **searcher_kwargs,
) -> None:
    """Run a neural pipeline search.

    To parallelize:
        To run a neural pipeline search with multiple processes or machines,
        simply call run(.) multiple times (optionally on different machines). Make sure
        that root_directory points to the same folder on the same filesystem, otherwise,
        the multiple calls to run(.) will be independent.

    Args:
        run_pipeline: The objective function to minimize.
        pipeline_space: The search space to minimize over.
        root_directory: The directory to save progress to. This is also used to
            synchronize multiple calls to run(.) for parallelization.
        overwrite_working_directory: If true, delete the working directory at the start of
            the run. This is, e.g., useful when debugging a run_pipeline function.
        post_run_summary: If True, creates a csv file after each worker is done,
            holding summary information about the configs and results.
        development_stage_id: ID for the current development stage. Only needed if
            you work with multiple development stages.
        task_id: ID for the current task. Only needed if you work with multiple
            tasks.
        max_evaluations_total: Number of evaluations after which to terminate.
        max_evaluations_per_run: Number of evaluations the specific call to run(.) should
            maximally do.
        continue_until_max_evaluation_completed: If true, only stop after
            max_evaluations_total have been completed. This is only relevant in the
            parallel setting.
        max_cost_total: No new evaluations will start when this cost is exceeded. Requires
            returning a cost in the run_pipeline function, e.g.,
            `return dict(loss=loss, cost=cost)`.
        ignore_errors: Ignore hyperparameter settings that threw an error and do not raise
            an error. Error configs still count towards max_evaluations_total.
        loss_value_on_error: Setting this and cost_value_on_error to any float will
            supress any error and will use given loss value instead. default: None
        cost_value_on_error: Setting this and loss_value_on_error to any float will
            supress any error and will use given cost value instead. default: None
        pre_load_hooks: List of functions that will be called before load_results().
        searcher: Which optimizer to use. This is usually only needed by neps developers.
        searcher_path: The path to the user created searcher. None when the user
            is using NePS designed searchers.
        **searcher_kwargs: Will be passed to the searcher. This is usually only needed by
            neps develolpers.

    Raises:
        ValueError: If deprecated argument working_directory is used.
        ValueError: If root_directory is None.
        TypeError: If pipeline_space has invalid type.


    Example:
        >>> import neps

        >>> def run_pipeline(some_parameter: float):
        >>>    validation_error = -some_parameter
        >>>    return validation_error

        >>> pipeline_space = dict(some_parameter=neps.FloatParameter(lower=0, upper=1))

        >>> logging.basicConfig(level=logging.INFO)
        >>> neps.run(
        >>>    run_pipeline=run_pipeline,
        >>>    pipeline_space=pipeline_space,
        >>>    root_directory="usage_example",
        >>>    max_evaluations_total=5,
        >>> )
    """
    if "working_directory" in searcher_kwargs:
        raise ValueError(
            "The argument 'working_directory' is deprecated, please use 'root_directory' "
            "instead"
        )

    if "budget" in searcher_kwargs:
        warnings.warn(
            "The argument: 'budget' is deprecated. In the neps.run call, please, use "
            "'max_cost_total' instead. In future versions using `budget` will fail.",
            DeprecationWarning,
            stacklevel=2,
        )
        max_cost_total = searcher_kwargs["budget"]
        del searcher_kwargs["budget"]
    
    logger = logging.getLogger("neps")
    logger.info(f"Starting neps.run using root directory {root_directory}")
    
    if isinstance(searcher, BaseOptimizer):
        searcher_instance = searcher
        searcher_name = "custom"
        searcher_alg = searcher.whoami()
        user_defined_searcher = True
    else:
        (   
            searcher_name,
            searcher_instance, 
            searcher_alg, 
            searcher_config, 
            searcher_info, 
            user_defined_searcher
        ) = _run_args(
            pipeline_space=pipeline_space,
            max_cost_total=max_cost_total,
            ignore_errors=ignore_errors,
            loss_value_on_error=loss_value_on_error,
            cost_value_on_error=cost_value_on_error,
            logger=logger,
            searcher=searcher,
            searcher_path=searcher_path,
            **searcher_kwargs,
        )

    # Used to create the yaml holding information about the searcher.
    # Also important for testing and debugging the api.
    searcher_info = {
        "searcher_name": searcher_name,
        "searcher_alg": searcher_alg,
        "user_defined_searcher": user_defined_searcher,
        "searcher_args_user_modified": False,
    }

    # Check to verify if the target directory contains the history of another optimizer state
    # This check is performed only when the `searcher` is built during the run
    if isinstance(searcher, BaseOptimizer):
        # This check is not strict when a user-defined neps.optimizer is provided
        logger.warn(
            "An instantiated optimizer is provided. The safety checks of NePS will be "
            "skipped. Accurate continuation of runs can no longer be guaranteed!"
        )
    elif isinstance(searcher, str):
        # Updating searcher arguments from searcher_kwargs
        for key, value in searcher_kwargs.items():
            if user_defined_searcher:
                if key not in searcher_config or searcher_config[key] != value:
                    searcher_config[key] = value
                    logger.info(
                        f"Updating the current searcher argument '{key}'"
                        f" with the value '{value}'"
                    )
                else:
                    logger.info(
                        f"The searcher argument '{key}' has the same"
                        f" value '{value}' as default."
                    )
                searcher_info["searcher_args_user_modified"] = True
            else:
                # No searcher argument updates when NePS decides the searcher.
                logger.info(35 * "=" + "WARNING" + 35 * "=")
                logger.info("CHANGING ARGUMENTS ONLY WORK WHEN SEARCHER IS DEFINED")
                logger.info(
                    f"The searcher argument '{key}' will not change to '{value}'"
                    f" because NePS chose the searcher"
                )
                searcher_info["searcher_args_user_modified"] = False
    else:
        raise ValueError(f"Unrecognized `searcher`. Not str or BaseOptimizer.")
    
    metahyper.run(
        run_pipeline,
        searcher_instance,
        searcher_info,
        root_directory,
        max_evaluations_total=max_evaluations_total,
        max_evaluations_per_run=max_evaluations_per_run,
        continue_until_max_evaluation_completed=continue_until_max_evaluation_completed,
        development_stage_id=development_stage_id,
        task_id=task_id,
        logger=logger,
        post_evaluation_hook=_post_evaluation_hook_function(
            loss_value_on_error, ignore_errors
        ),
        overwrite_optimization_dir=overwrite_working_directory,
        pre_load_hooks=pre_load_hooks,
    )

    if post_run_csv:
        post_run_csv(root_directory, logger)


def _run_args(
    pipeline_space: dict[str, Parameter | CS.ConfigurationSpace] | CS.ConfigurationSpace,
    max_cost_total: int | float | None = None,
    ignore_errors: bool = False,
    loss_value_on_error: None | float = None,
    cost_value_on_error: None | float = None,
    logger=None,
    searcher: Literal[
        "default",
        "bayesian_optimization",
        "random_search",
        "hyperband",
        "priorband",
        "mobster",
        "asha",
        "regularized_evolution",
    ]
    | BaseOptimizer = "default",
    searcher_path: Path | str | None = None,
    **searcher_kwargs,
) -> None:
    try:
        # Support pipeline space as ConfigurationSpace definition
        if isinstance(pipeline_space, CS.ConfigurationSpace):
            pipeline_space = pipeline_space_from_configspace(pipeline_space)

        # Support pipeline space as mix of ConfigurationSpace and neps parameters
        new_pipeline_space: dict[str, Parameter] = dict()
        for key, value in pipeline_space.items():
            if isinstance(value, CS.ConfigurationSpace):
                config_space_parameters = pipeline_space_from_configspace(value)
                new_pipeline_space = {**new_pipeline_space, **config_space_parameters}
            else:
                new_pipeline_space[key] = value
        pipeline_space = new_pipeline_space
        
        # Transform to neps internal representation of the pipeline space
        pipeline_space = SearchSpace(**pipeline_space)
    except TypeError as e:
        message = f"The pipeline_space has invalid type: {type(pipeline_space)}"
        raise TypeError(message) from e

    user_defined_searcher = False

    if isinstance(searcher, str) and searcher_path is not None:
        # The users has their own custom searcher.
        logging.info("Preparing to run user created searcher")

        config = get_searcher_data(searcher, searcher_path)
        user_defined_searcher = True
    else:
        if searcher in ["default", None]:
            # NePS decides the searcher according to the pipeline space.
            if pipeline_space.has_prior:
                searcher = "priorband" if pipeline_space.has_fidelity else "pibo"
            else:
                searcher = (
                    "hyperband"
                    if pipeline_space.has_fidelity
                    else "bayesian_optimization"
                )
        else:
            # Users choose one of NePS searchers.
            user_defined_searcher = True
        # Fetching the searcher data, throws an error when the searcher is not found
        config = get_searcher_data(searcher)

    searcher_alg = config["searcher_init"]["algorithm"]
    searcher_config = {} if config["searcher_kwargs"] is None else config["searcher_kwargs"]

    logger.info(f"Running {searcher} as the searcher")
    logger.info(f"Algorithm: {searcher_alg}")

    # Used to create the yaml holding information about the searcher.
    # Also important for testing and debugging the api.
    searcher_info = {
        "searcher_name": searcher,
        "searcher_alg": searcher_alg,
        "user_defined_searcher": user_defined_searcher,
        "searcher_args_user_modified": False,
    }

    # Updating searcher arguments from searcher_kwargs
    for key, value in searcher_kwargs.items():
        if user_defined_searcher:
            if key not in searcher_config or searcher_config[key] != value:
                searcher_config[key] = value
                logger.info(
                    f"Updating the current searcher argument '{key}'"
                    f" with the value '{value}'"
                )
            else:
                logger.info(
                    f"The searcher argument '{key}' has the same"
                    f" value '{value}' as default."
                )
            searcher_info["searcher_args_user_modified"] = True
        else:
            # No searcher argument updates when NePS decides the searcher.
            logger.info(35 * "=" + "WARNING" + 35 * "=")
            logger.info("CHANGINE ARGUMENTS ONLY WORKS WHEN SEARCHER IS DEFINED")
            logger.info(
                f"The searcher argument '{key}' will not change to '{value}'"
                f" because NePS chose the searcher"
            )
            searcher_info["searcher_args_user_modified"] = False

    searcher_config.update(
        {
            "loss_value_on_error": loss_value_on_error,
            "cost_value_on_error": cost_value_on_error,
            "ignore_errors": ignore_errors,
        }
    )
    
    searcher_instance = instance_from_map(
        SearcherMapping, searcher_alg, "searcher", as_class=True
    )(
        pipeline_space=pipeline_space,
        budget=max_cost_total,  # TODO: use max_cost_total everywhere
        **searcher_config,
    )
    
    return searcher, searcher_instance, searcher_alg, searcher_config, searcher_info, user_defined_searcher