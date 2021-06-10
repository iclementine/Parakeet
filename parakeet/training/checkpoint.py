# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Callable, Mapping, List, Union
import os
from pathlib import Path


class KBest(object):
    """
    A utility class to help save the hard drive by only keeping K best 
    checkpoints. 
    
    To be as modularized as possible, this class does not assume anything like 
    a Trainer class or anything like a checkpoint directory, it does not know 
    about the model or the optimizer, etc. 
    
    It is basically a dynamically mantained K-bset Mapping. When a new item is 
    added to the map, save_fn is called. And when an item is removed from the 
    map, del_fn is called. `save_fn` and `del_fn` takes a Path object as input 
    and returns nothing.

    Though it is designed to control checkpointing behaviors, it can be used 
    to do something else if you pass some save_fn and del_fn.

    Example
    --------

    >>> from pathlib import Path
    >>> import shutil
    >>> import paddle
    >>> from paddle import nn
    
    >>> model = nn.Linear(2, 3)
    >>> def save_model(path):
    ...     paddle.save(model.state_dict(), path)

    >>> kbest_manager = KBest(max_size=5, save_fn=save_model)
    >>> checkpoint_dir = Path("checkpoints")
    >>> shutil.rmtree(checkpoint_dir)
    >>> checkpoint_dir.mkdir(parents=True)
    >>> a = np.random.rand(20)
    >>> for i, score in enumerate(a):
    ...     path = checkpoint_dir / f"step_{i}"
    ...     kbest_manager.add_checkpoint(score, path)
    >>> assert len(list(checkpoint_dir.glob("step_*"))) == 5
    """

    def __init__(self,
                 max_size: int=5,
                 save_fn: Callable[[Union[Path, str]], None]=None,
                 del_fn: Callable[[Union[Path, str]], None]=os.remove):
        self.best_records: Mapping[Path, float] = {}
        self.save_fn = save_fn
        self.del_fn = del_fn
        self.max_size = max_size
        self._save_all = (max_size == -1)

    def should_save(self, metric: float) -> bool:
        if not self.full():
            return True

        # already full
        worst_record_path = max(self.best_records, key=self.best_records.get)
        worst_metric = self.best_records[worst_record_path]
        return metric < worst_metric

    def full(self):
        return (not self._save_all) and len(self.best_records) == self.max_size

    def add_checkpoint(self, metric, path):
        if self.should_save(metric):
            self.save_checkpoint_and_update(metric, path)

    def save_checkpoint_and_update(self, metric, path):
        # remove the worst
        if self.full():
            worst_record_path = max(self.best_records,
                                    key=self.best_records.get)
            self.best_records.pop(worst_record_path)
            self.del_fn(worst_record_path)

        # add the new one
        self.save_fn(path)
        self.best_records[path] = metric


class KLatest(object):
    """
    A utility class to help save the hard drive by only keeping K latest 
    checkpoints. 
    
    To be as modularized as possible, this class does not assume anything like 
    a Trainer class or anything like a checkpoint directory, it does not know 
    about the model or the optimizer, etc. 
    
    It is basically a dynamically mantained Queue. When a new item is 
    added to the queue, save_fn is called. And when an item is removed from the 
    queue, del_fn is called. `save_fn` and `del_fn` takes a Path object as input 
    and returns nothing.

    Though it is designed to control checkpointing behaviors, it can be used 
    to do something else if you pass some save_fn and del_fn.

    Example
    --------

    >>> from pathlib import Path
    >>> import shutil
    >>> import paddle
    >>> from paddle import nn
    
    >>> model = nn.Linear(2, 3)
    >>> def save_model(path):
    ...     paddle.save(model.state_dict(), path)

    >>> klatest_manager = KLatest(max_size=5, save_fn=save_model)
    >>> checkpoint_dir = Path("checkpoints")
    >>> shutil.rmtree(checkpoint_dir)
    >>> checkpoint_dir.mkdir(parents=True)
    >>> for i in range(20):
    ...     path = checkpoint_dir / f"step_{i}"
    ...     klatest_manager.add_checkpoint(path)
    >>> assert len(list(checkpoint_dir.glob("step_*"))) == 5
    """

    def __init__(self,
                 max_size: int=5,
                 save_fn: Callable[[Path], None]=None,
                 del_fn: Callable[[Path], None]=lambda f: f.unlink()):
        self.latest_records: List[Path] = []
        self.save_fn = save_fn
        self.del_fn = del_fn
        self.max_size = max_size
        self._save_all = (max_size == -1)

    def full(self):
        return (
            not self._save_all) and len(self.latest_records) == self.max_size

    def add_checkpoint(self, path):
        self.save_checkpoint_and_update(path)

    def save_checkpoint_and_update(self, path):
        # remove the earist
        if self.full():
            eariest_record_path = self.latest_records.pop(0)
            self.del_fn(eariest_record_path)

        # add the new one
        self.save_fn(path)
        self.latest_records.append(path)