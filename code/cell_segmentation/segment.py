"""
Main file to run segmentation
"""

import os

from cellpose_segmentation.combine_gradients import combine_gradients
from cellpose_segmentation.predict_gradients import predict_gradients


def segment():
    """
    Segments a Z1 dataset.
    """

    # Code ocean folders
    results_folder = os.path.abspath("../results")
    data_folder = os.path.abspath("../data")

    IMAGE_PATH = "HCR_BL6-000_2023-06-1_00-00-00_fused_2024-02-09_13-28-49"
    TILE_NAME = "channel_405.zarr"
    # dataset_path = f"s3://{BUCKET_NAME}/{IMAGE_PATH}/{TILE_NAME}"
    dataset_path = f"/data/{IMAGE_PATH}/{TILE_NAME}"

    # Data loader params
    super_chunksize = None
    target_size_mb = 3072  # None
    n_workers = 16
    batch_size = 1

    # Cellpose params
    model_name = "cyto"
    normalize_image = True  # TODO Normalize image in the entire dataset
    cell_diameter = 15

    slices_per_axis = [40, 80, 80]

    # output gradients
    output_gradients_path = f"{results_folder}/gradients.zarr"

    # Large-scale prediction of gradients
    predict_gradients(
        dataset_path=dataset_path,
        multiscale="2",
        output_gradients_path=output_gradients_path,
        slices_per_axis=slices_per_axis,
        target_size_mb=target_size_mb,
        n_workers=n_workers,
        batch_size=batch_size,
        super_chunksize=super_chunksize,
        normalize_image=normalize_image,
        model_name=model_name,
        cell_diameter=cell_diameter,
    )

    # output combined gradients path and cell probabilities
    output_combined_gradients_path = f"{results_folder}/combined_gradients.zarr"
    output_cellprob_path = "../results/combined_cellprob.zarr"

    # Large-scale combination of predicted gradients
    combine_gradients(
        dataset_path=output_gradients_path,
        multiscale=".",
        output_combined_gradients_path=output_combined_gradients_path,
        output_cellprob_path=output_cellprob_path,
        prediction_chunksize=(3, 3, 128, 128, 128),
        super_chunksize=(3, 3, 128, 128, 128),
        target_size_mb=target_size_mb,
        n_workers=0,
        batch_size=1,
    )