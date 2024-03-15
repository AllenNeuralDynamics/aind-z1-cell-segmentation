"""
3D combination of gradients that were previously
predicted. This is a local operation that happens
that does not require to run in overlapping chunks.
"""

import multiprocessing
from time import time
from typing import Callable, Optional, Tuple

import numpy as np
import psutil
import zarr
from aind_large_scale_prediction._shared.types import ArrayLike, PathLike
from aind_large_scale_prediction.generator.dataset import create_data_loader
from aind_large_scale_prediction.generator.utils import recover_global_position

from ..utils import utils


def combine_gradients(
    dataset_path: PathLike,
    multiscale: str,
    output_combined_gradients_path: PathLike,
    output_cellprob_path: PathLike,
    prediction_chunksize: Tuple[int, ...],
    target_size_mb: int,
    n_workers: int,
    batch_size: int,
    super_chunksize: Tuple[int, ...],
    results_folder: PathLike,
    lazy_callback_fn: Optional[Callable[[ArrayLike], ArrayLike]] = None,
):
    """
    Local 3D combination of predicted gradients.
    This operation is necessary before following
    the flows to the centers identified cells.

    Parameters
    ----------
    dataset_path: str
        Path where the zarr dataset is stored. It could
        be a local path or in a S3 path.

    multiscale: str
        Multiscale to process

    output_combined_gradients_path: PathLike
        Path where we want to output the combined gradients.

    output_cellprob_path: PathLike
        Path where we want to output the cell proabability
        maps. It is not completely necessary to save them
        but it is good for quality control.

    prediction_chunksize: Tuple[int, ...]
        Prediction chunksize.

    target_size_mb: int
        Target size in megabytes the data loader will
        load in memory at a time

    n_workers: int
        Number of workers that will concurrently pull
        data from the shared super chunk in memory

    batch_size: int
        Batch size

    super_chunksize: Optional[Tuple[int, ...]]
        Super chunk size that will be in memory at a
        time from the raw data. If provided, then
        target_size_mb is ignored. Default: None

    """

    co_cpus = int(utils.get_code_ocean_cpu_limit())

    if n_workers > co_cpus:
        raise ValueError(f"Provided workers {n_workers} > current workers {co_cpus}")

    logger = utils.create_logger(output_log_path=results_folder, mode="a")
    logger.info(f"{20*'='} Z1 Large-Scale Cellpose Combination of Gradients {20*'='}")

    utils.print_system_information(logger)

    logger.info(f"Processing dataset {dataset_path}")

    # Tracking compute resources
    # Subprocess to track used resources
    manager = multiprocessing.Manager()
    time_points = manager.list()
    cpu_percentages = manager.list()
    memory_usages = manager.list()

    profile_process = multiprocessing.Process(
        target=utils.profile_resources,
        args=(
            time_points,
            cpu_percentages,
            memory_usages,
            20,
        ),
    )
    profile_process.daemon = True
    profile_process.start()

    ## Creating zarr data loader
    logger.info("Creating chunked data loader")
    shm_memory = psutil.virtual_memory()
    logger.info(f"Shared memory information: {shm_memory}")

    # The device we will use and pinning memory to speed things up
    device = None

    pin_memory = True
    if device is not None:
        pin_memory = False
        multiprocessing.set_start_method("spawn", force=True)

    # Getting overlap prediction chunksize
    overlap_prediction_chunksize = (
        0,
        0,
        0,
        0,
        0,
    )
    logger.info(
        f"Overlap size based on cell diameter * 2: {overlap_prediction_chunksize}"
    )

    # Creation of zarr data loader
    zarr_data_loader, zarr_dataset = create_data_loader(
        dataset_path=dataset_path,
        multiscale=multiscale,
        target_size_mb=target_size_mb,
        prediction_chunksize=prediction_chunksize,
        overlap_prediction_chunksize=overlap_prediction_chunksize,
        n_workers=n_workers,
        batch_size=batch_size,
        dtype=np.float32,  # Allowed data type to process with pytorch cuda
        super_chunksize=super_chunksize,
        lazy_callback_fn=None,  # partial_lazy_deskewing,
        logger=logger,
        device=device,
        pin_memory=pin_memory,
        override_suggested_cpus=False,
        drop_last=True,
        locked_array=False,
    )

    logger.info(f"Creating zarr gradients in path: {output_combined_gradients_path}")
    output_combined_gradients = zarr.open(
        output_combined_gradients_path,
        "w",
        shape=(3,) + zarr_dataset.lazy_data.shape[-3:],  # dZ, dY, dX
        chunks=(1,) + tuple(prediction_chunksize[-3:]),
        dtype=np.float32,
    )

    output_cellprob = zarr.open(
        output_cellprob_path,
        "w",
        shape=zarr_dataset.lazy_data.shape[-3:],
        chunks=tuple(prediction_chunksize[-3:]),
        dtype=np.uint8,
    )
    logger.info(
        f"Combined gradients: {output_combined_gradients} - chunks: {output_combined_gradients.chunks}"
    )
    logger.info(
        f"Cell probabilities path: {output_cellprob} - chunks: {output_cellprob.chunks}"
    )

    # Estimating total batches
    total_batches = np.prod(zarr_dataset.lazy_data.shape) / (
        np.prod(zarr_dataset.prediction_chunksize) * batch_size
    )
    samples_per_iter = n_workers * batch_size
    logger.info(
        f"Number of batches: {total_batches} - Samples per iteration: {samples_per_iter}"
    )

    logger.info(f"{20*'='} Starting combination of gradients {20*'='}")
    start_time = time()

    cellprob_threshold = 0.0

    for i, sample in enumerate(zarr_data_loader):
        logger.info(
            f"Batch {i}: {sample.batch_tensor.shape} - Pinned?: {sample.batch_tensor.is_pinned()} - dtype: {sample.batch_tensor.dtype} - device: {sample.batch_tensor.device}"
        )

        # Recover global position of internal chunk
        (
            global_coord_pos,
            global_coord_positions_start,
            global_coord_positions_end,
        ) = recover_global_position(
            super_chunk_slice=sample.batch_super_chunk[0],
            internal_slices=sample.batch_internal_slice,
        )

        data = np.squeeze(sample.batch_tensor.numpy(), axis=0)

        dP = np.stack(
            (
                data[1][0] + data[2][0],  # dZ
                data[0][0] + data[2][1],  # dY
                data[0][1] + data[1][1],  # dX
            ),
            axis=0,
        )

        # Cell probability above threshold
        cell_probability = (
            data[0][-1] + data[1][-1] + data[2][-1] > cellprob_threshold
        ).astype(np.uint8)

        # Looking at flows within cell areas
        dP_masked = dP * cell_probability

        # Saving cell probability as binary mask
        cellprob_coord_pos = global_coord_pos[-3:]
        # print("Save cell prob coords: ", cellprob_coord_pos)
        output_cellprob[cellprob_coord_pos] = cell_probability

        # Saving dP
        combined_gradients_coord_pos = (slice(0, 3),) + global_coord_pos[-3:]
        # print("Save combined gradients coords: ", combined_gradients_coord_pos)
        output_combined_gradients[combined_gradients_coord_pos] = dP_masked

        logger.info(
            f"Cell probability coords: {cellprob_coord_pos} - dP masked coords: {combined_gradients_coord_pos}"
        )

    end_time = time()

    logger.info(f"Processing time: {end_time - start_time} seconds")

    # Getting tracked resources and plotting image
    utils.stop_child_process(profile_process)

    if len(time_points):
        utils.generate_resources_graphs(
            time_points,
            cpu_percentages,
            memory_usages,
            results_folder,
            "cellpose_combine_gradients",
        )


def main():
    """Main function"""
    combine_gradients(
        dataset_path="../results/gradients.zarr",
        multiscale=".",
        output_combined_gradients_path="../results/combined_gradients.zarr",
        output_cellprob_path="../results/combined_cellprob.zarr",
        prediction_chunksize=(3, 3, 128, 128, 128),
        super_chunksize=(3, 3, 128, 128, 128),
        target_size_mb=2048,
        n_workers=0,
        batch_size=1,
    )


if __name__ == "__main__":
    main()
