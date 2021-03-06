from concurrent.futures import ProcessPoolExecutor
import datetime
import msgpack
import numba
import numpy as np
import os.path
import pandas as pd
import warnings
import zipfile

from ..db import find_interactions_in_frame, get_frame_metadata, get_frames

@numba.njit
def calculate_head_distance(xy0, xy1):
    head0 = xy0[:2]
    head0[0] += 3.19 * np.cos(xy0[2])
    head0[1] += 3.19 * np.sin(xy0[2])
    head1 = xy1[:2]
    head1[0] += 3.19 * np.cos(xy1[2])
    head1[1] += 3.19 * np.sin(xy1[2])
    
    d = 0.0
    d = np.linalg.norm(head0 - head1)
    return d

@numba.njit
def calculate_angle_dot_distance(r0, r1):
    vec0 = np.array([np.cos(r0), np.sin(r0)])
    vec1 = np.array([np.cos(r1), np.sin(r1)])
    d = 0.0
    d = np.dot(vec0, vec1)
    return d
    
@numba.njit
def probability_distance_fun_(xy0, xy1, beta0, beta1, beta2, bias, hard_min, hard_max):

    distance = np.linalg.norm(xy0[:2] - xy1[:2])
    if distance <= hard_min or distance >= hard_max:
        return 0.0

    head_distance = calculate_head_distance(xy0, xy1)
    angle_dot_distance = calculate_angle_dot_distance(xy0[2], xy1[2])
    
    distance = distance * beta0 + head_distance * beta1 + angle_dot_distance * beta2 + bias
    distance = (1.0 / (1.0 + np.exp(-distance)))
    return distance

@numba.njit
def probability_distance_fun_vectorized_(xy0, xy1, out):
    out = np.zeros(shape=(xy0.shape[0],), dtype=np.float32)
    for i in range(xy0.shape[0]):
        out[i] = probability_distance_fun(xy0[i,:], xy1[i,:])
        
@numba.jit
def probability_distance_fun_vectorized(xy0, xy1):
    out = np.zeros(shape=(xy0.shape[0],), dtype=np.float32)
    return probability_distance_fun_vectorized_(xy0, xy1, out)

def get_data_for_frame_id(timestamp, frame_id, cam_id,
                                  max_distance, min_distance, distance_func,
                                  thread_context=None, **kwargs):
    r = find_interactions_in_frame(
            frame_id, max_distance=max_distance, min_distance=min_distance,
            distance_func=distance_func,
                features=["x_pos_hive", "y_pos_hive", "orientation_hive"],
            cursor=thread_context, cursor_is_prepared=thread_context is not None)
    
    core_data = [i[:3] for i in r]
    core_data = pd.DataFrame(core_data, columns=("frame_id", "bee_id0", "bee_id1"), dtype=np.uint64)
    return timestamp, frame_id, cam_id, core_data

@numba.njit
def probability_distance_fun(xy0, xy1):
    return probability_distance_fun_(xy0, xy1, 2.04332357, -1.56938987, 1.92212738, -11.87978937,
                                   6.843017734527588, 28.133578964233394)
high_recall_threshold = 0.45398181  # 85% recall, 18% precision

def get_data_for_frame_id_high_recall(*args, **kwargs):
    return get_data_for_frame_id(*args, 
                     max_distance=2.0, min_distance=high_recall_threshold,
                     distance_func=probability_distance_fun,
                     **kwargs)

def get_available_processed_days(base_path=None):
    import glob
    if base_path is None:
        base_path = "/mnt/storage/david/cache/beesbook/trophallaxis"
    available_files = set()
    for ext in ("zip", ):
        available_files |= set(glob.glob(base_path + "/prefilter.*." + ext))
    
    available_files = list(sorted(list(available_files)))
    available_files_df = []
    for filename in available_files:
        leaf_name = filename.split("/")[-1]
        infos = leaf_name.split(".")[1]
        infos = infos.split("_")
        cam_id = int(infos[0])
        datetime_from, datetime_to = [datetime.datetime.strptime(dt.split("+")[0], "%Y-%m-%d %H:%M:%S") for dt in infos[1:]]
        available_files_df.append(dict(
            cam_id=cam_id,
            begin=datetime_from,
            end=datetime_to,
            filename=filename
        ))
        
    available_files_df = pd.DataFrame(available_files_df)
    return available_files_df

def load_processed_data(f, warnings_as_errors=False):
    import msgpack
    if type(f) is str:
        if f.endswith(".zip"):
            import zipfile
            with zipfile.ZipFile(f, "r") as zf:
                file = zf.open(f.split("/")[-1].replace(".zip", ".msgpack"))
                return load_processed_data(file, warnings_as_errors=warnings_as_errors)
        elif f.endswith(".msgpack"):
            with open(f, "rb") as file:
                return load_processed_data(file, warnings_as_errors=warnings_as_errors)

    try:
        data = msgpack.load(f, max_array_len=2147483647)
    except Exception as e:
        print("Error unpickling {}!".format(str(f)))
        print(str(e))
        return None

    if not data:
        return None
    data = pd.DataFrame(data, columns=["frame_id", "bee_id0", "bee_id1"], dtype=np.uint64)    
    all_frame_ids = data.frame_id.unique()
    try:
        metadata = get_frame_metadata(all_frame_ids, warnings_as_errors=warnings_as_errors)
    except Exception as e:
        raise e
    metadata.frame_id = metadata.frame_id.astype(np.uint64)
    metadata["datetime"] = pd.to_datetime(metadata.timestamp, unit="s")
    
    data = data.merge(metadata, on="frame_id", how="inner")
    return data

def load_all_processed_data(paths):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as executor:
        data = executor.map(load_processed_data, paths)
    return pd.concat([d for d in data if d is not None], axis=0, ignore_index=True)

def process_frame_with_prefilter(frame_info):
    results = get_data_for_frame_id_high_recall(*frame_info)
    return results
            
def prefilter_data_for_timerange(dt_from, dt_to, target_dir=None, progress="tqdm"):
    import cloudpickle
    
    if progress is None:
        progress = lambda x=None, **kwargs: x
    else:
        import tqdm
        if progress == "tqdm":
            progress = tqdm.tqdm
        elif progress == "tqdm_notebook":
            progress = tqdm.tqdm_notebook

    if target_dir is None:
        target_dir = "/mnt/storage/david/cache/beesbook/trophallaxis/"
    trange = progress(total=(dt_to-dt_from).days, desc="Days")
    skipped = 0
    current_day_start = dt_from
    while True:
        if current_day_start > dt_to:
            break
        current_day_end = current_day_start + datetime.timedelta(days=1)

        for cam_id in progress(range(4), desc="Cam ID", leave=False):

            def dt_to_string(cam, dt, dt_to):
                return target_dir + "/prefilter.{}_{}_{}.zip".format(
                    cam, str(dt), str(dt_to))
            output_filename = dt_to_string(cam_id, current_day_start, current_day_end)
            if os.path.isfile(output_filename):
                skipped += 1
                if trange is not None:
                    trange.set_postfix(dict(skipped=skipped))
                continue

            def iter_frames_to_filter(cam_id, from_, to_):
                all_frames = list(get_frames(cam_id, from_.timestamp(), to_.timestamp()))

                for idx, (timestamp, frame_id, cam_id) in enumerate(all_frames):
                    if idx % 3 == 0:
                        yield (timestamp, frame_id, cam_id)

            def data_source():
                yield from progress(iter_frames_to_filter(cam_id, current_day_start, current_day_end), leave=False, desc="Frames")

            all_interaction_results = dict()
            all_failed_keys = []
            result_progress = progress(leave=False, desc="Results")
            def save_data(results):
                nonlocal all_interaction_results
                nonlocal all_failed_keys
                timestamp, frame_id, cam_id, core_data = results
                if len(core_data) == 0:
                    all_failed_keys.append(results[:-1])
                else:
                    all_interaction_results[(cam_id, timestamp, frame_id)] = core_data
                result_progress.set_postfix(dict(failed_keys=len(all_failed_keys)))
                result_progress.update()

            executor = ProcessPoolExecutor(max_workers=32)
            for result in executor.map(process_frame_with_prefilter, data_source()):
                save_data(result)

            if len(all_failed_keys) > 0:
                with open(output_filename + ".failed.cloudpickle", "wb") as f:
                    cloudpickle.dump(all_failed_keys, f)
                warnings.warn("Found {} frame IDs without interaction data! Saved for debugging..".format(len(all_failed_keys)))

            if len(all_interaction_results) == 0:
                continue

            data_df = pd.concat(all_interaction_results.values())
            assert (data_df.frame_id.dtype == np.uint64)
            data_df.frame_id = data_df.frame_id.astype(np.uint64)
            data_df.bee_id0 = data_df.bee_id0.astype(np.uint16)
            data_df.bee_id1 = data_df.bee_id1.astype(np.uint16)

            raw_df = list(data_df.itertuples(index=False))

            with zipfile.ZipFile(output_filename, "w", zipfile.ZIP_DEFLATED) as zf:
                with zf.open(output_filename.split("/")[-1].replace("zip", "msgpack"), "w") as file:
                    msgpack.dump(raw_df, file, use_bin_type=True)

            result_progress.close()

        if trange is not None:
            trange.update()
        current_day_start = current_day_end