import os
import pickle
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

import einops
import numpy as np
import pygame
import pymunk
import torch
import torchrl
import tqdm
from diffusion_policy.common.replay_buffer import ReplayBuffer
from diffusion_policy.env.pusht.pusht_env import pymunk_to_shapely
from tensordict import TensorDict
from torchrl.data.datasets.utils import _get_root_dir
from torchrl.data.replay_buffers.replay_buffers import (
    TensorDictPrioritizedReplayBuffer,
    TensorDictReplayBuffer,
)
from torchrl.data.replay_buffers.samplers import (
    Sampler,
    SliceSampler,
    SliceSamplerWithoutReplacement,
)
from torchrl.data.replay_buffers.storages import TensorStorage, _collate_id
from torchrl.data.replay_buffers.writers import ImmutableDatasetWriter, Writer

# as define in env
SUCCESS_THRESHOLD = 0.95  # 95% coverage,


def get_goal_pose_body(pose):
    mass = 1
    inertia = pymunk.moment_for_box(mass, (50, 100))
    body = pymunk.Body(mass, inertia)
    # preserving the legacy assignment order for compatibility
    # the order here doesn't matter somehow, maybe because CoM is aligned with body origin
    body.position = pose[:2].tolist()
    body.angle = pose[2]
    return body


def add_segment(space, a, b, radius):
    shape = pymunk.Segment(space.static_body, a, b, radius)
    shape.color = pygame.Color("LightGray")  # https://htmlcolorcodes.com/color-names
    return shape


def add_tee(
    space,
    position,
    angle,
    scale=30,
    color="LightSlateGray",
    mask=pymunk.ShapeFilter.ALL_MASKS(),
):
    mass = 1
    length = 4
    vertices1 = [
        (-length * scale / 2, scale),
        (length * scale / 2, scale),
        (length * scale / 2, 0),
        (-length * scale / 2, 0),
    ]
    inertia1 = pymunk.moment_for_poly(mass, vertices=vertices1)
    vertices2 = [
        (-scale / 2, scale),
        (-scale / 2, length * scale),
        (scale / 2, length * scale),
        (scale / 2, scale),
    ]
    inertia2 = pymunk.moment_for_poly(mass, vertices=vertices1)
    body = pymunk.Body(mass, inertia1 + inertia2)
    shape1 = pymunk.Poly(body, vertices1)
    shape2 = pymunk.Poly(body, vertices2)
    shape1.color = pygame.Color(color)
    shape2.color = pygame.Color(color)
    shape1.filter = pymunk.ShapeFilter(mask=mask)
    shape2.filter = pymunk.ShapeFilter(mask=mask)
    body.center_of_gravity = (shape1.center_of_gravity + shape2.center_of_gravity) / 2
    body.position = position
    body.angle = angle
    body.friction = 1
    space.add(body, shape1, shape2)
    return body


class PushtExperienceReplay(TensorDictReplayBuffer):

    # available_datasets = [
    #     "xarm_lift_medium",
    # ]

    def __init__(
        self,
        dataset_id,
        batch_size: int = None,
        *,
        shuffle: bool = True,
        num_slices: int = None,
        slice_len: int = None,
        pad: float = None,
        replacement: bool = None,
        streaming: bool = False,
        root: Path = None,
        download: bool = False,
        sampler: Sampler = None,
        writer: Writer = None,
        collate_fn: Callable = None,
        pin_memory: bool = False,
        prefetch: int = None,
        transform: "torchrl.envs.Transform" = None,  # noqa-F821
        split_trajs: bool = False,
        strict_length: bool = True,
    ):
        self.download = download
        if streaming:
            raise NotImplementedError
        self.streaming = streaming
        self.dataset_id = dataset_id
        self.split_trajs = split_trajs
        self.shuffle = shuffle
        self.num_slices = num_slices
        self.slice_len = slice_len
        self.pad = pad

        self.strict_length = strict_length
        if (self.num_slices is not None) and (self.slice_len is not None):
            raise ValueError("num_slices or slice_len can be not None, but not both.")
        if split_trajs:
            raise NotImplementedError

        if self.download == True:
            raise NotImplementedError()

        if root is None:
            root = _get_root_dir("pusht")
            os.makedirs(root, exist_ok=True)
        self.root = Path(root)
        if self.download == "force" or (self.download and not self._is_downloaded()):
            storage = self._download_and_preproc()
        else:
            storage = TensorStorage(TensorDict.load_memmap(self.root / dataset_id))

        # if num_slices is not None or slice_len is not None:
        #     if sampler is not None:
        #         raise ValueError(
        #             "`num_slices` and `slice_len` are exclusive with the `sampler` argument."
        #         )

        #     if replacement:
        #         if not self.shuffle:
        #             raise RuntimeError(
        #                 "shuffle=False can only be used when replacement=False."
        #             )
        #         sampler = SliceSampler(
        #             num_slices=num_slices,
        #             slice_len=slice_len,
        #             strict_length=strict_length,
        #         )
        #     else:
        #         sampler = SliceSamplerWithoutReplacement(
        #             num_slices=num_slices,
        #             slice_len=slice_len,
        #             strict_length=strict_length,
        #             shuffle=self.shuffle,
        #         )

        if writer is None:
            writer = ImmutableDatasetWriter()
        if collate_fn is None:
            collate_fn = _collate_id

        super().__init__(
            storage=storage,
            sampler=sampler,
            writer=writer,
            collate_fn=collate_fn,
            pin_memory=pin_memory,
            prefetch=prefetch,
            batch_size=batch_size,
            transform=transform,
        )

    @property
    def data_path_root(self):
        if self.streaming:
            return None
        return self.root / self.dataset_id

    def _is_downloaded(self):
        return os.path.exists(self.data_path_root)

    def _download_and_preproc(self):
        # download
        # TODO(rcadene)

        # load
        zarr_path = (
            "/home/rcadene/code/diffusion_policy/data/pusht/pusht_cchi_v7_replay.zarr"
        )
        dataset_dict = ReplayBuffer.copy_from_path(
            zarr_path
        )  # , keys=['img', 'state', 'action'])

        episode_ids = dataset_dict.get_episode_idxs()
        num_episodes = dataset_dict.meta["episode_ends"].shape[0]
        total_frames = dataset_dict["action"].shape[0]
        assert len(
            set([dataset_dict[key].shape[0] for key in dataset_dict.keys()])
        ), "Some data type dont have the same number of total frames."

        # TODO: verify that goal pose is expected to be fixed
        goal_pos_angle = np.array([256, 256, np.pi / 4])  # x, y, theta (in radians)
        goal_body = get_goal_pose_body(goal_pos_angle)

        idx0 = 0
        idxtd = 0
        for episode_id in tqdm.tqdm(range(num_episodes)):
            idx1 = dataset_dict.meta["episode_ends"][episode_id]

            num_frames = idx1 - idx0

            assert (episode_ids[idx0:idx1] == episode_id).all()

            image = torch.from_numpy(dataset_dict["img"][idx0:idx1])
            image = einops.rearrange(image, "b h w c -> b c h w")

            state = torch.from_numpy(dataset_dict["state"][idx0:idx1])
            agent_pos = state[:, :2]
            block_pos = state[:, 2:4]
            block_angle = state[:, 4]

            reward = torch.zeros(num_frames, 1)
            done = torch.zeros(num_frames, 1, dtype=torch.bool)
            for i in range(num_frames):
                space = pymunk.Space()
                space.gravity = 0, 0
                space.damping = 0

                # Add walls.
                walls = [
                    add_segment(space, (5, 506), (5, 5), 2),
                    add_segment(space, (5, 5), (506, 5), 2),
                    add_segment(space, (506, 5), (506, 506), 2),
                    add_segment(space, (5, 506), (506, 506), 2),
                ]
                space.add(*walls)

                block_body = add_tee(
                    space, block_pos[i].tolist(), block_angle[i].item()
                )
                goal_geom = pymunk_to_shapely(goal_body, block_body.shapes)
                block_geom = pymunk_to_shapely(block_body, block_body.shapes)
                intersection_area = goal_geom.intersection(block_geom).area
                goal_area = goal_geom.area
                coverage = intersection_area / goal_area
                reward[i] = np.clip(coverage / SUCCESS_THRESHOLD, 0, 1)
                done[i] = coverage > SUCCESS_THRESHOLD

            episode = TensorDict(
                {
                    ("observation", "image"): image[:-1],
                    ("observation", "state"): agent_pos[:-1],
                    "action": torch.from_numpy(dataset_dict["action"][idx0:idx1])[:-1],
                    "episode": torch.from_numpy(episode_ids[idx0:idx1])[:-1],
                    "frame_id": torch.arange(0, num_frames - 1, 1),
                    ("next", "observation", "image"): image[1:],
                    ("next", "observation", "state"): agent_pos[1:],
                    # TODO: verify that reward and done are aligned with image and agent_pos
                    ("next", "reward"): reward[1:],
                    ("next", "done"): done[1:],
                },
                batch_size=num_frames - 1,
            )

            if episode_id == 0:
                # hack to initialize tensordict data structure to store episodes
                td_data = (
                    episode[0]
                    .expand(total_frames)
                    .memmap_like(self.root / self.dataset_id)
                )

            td_data[idxtd : idxtd + len(episode)] = episode

            idx0 = idx1
            idxtd = idxtd + len(episode)

        return TensorStorage(td_data.lock_())
