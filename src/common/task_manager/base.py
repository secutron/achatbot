from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass
import multiprocessing
import threading
from typing import Dict


@dataclass
class Task:
    tid: str = ""
    name: str = ""
    tag: str = ""
    task: asyncio.Task | threading.Thread | multiprocessing.Process = None

    def __str__(self) -> str:
        return f"tid: {self.tid} name:{self.name} tag:{self.tag} is_alive:{self.is_alive()}"

    def is_alive(self) -> bool:
        if self.task is None:
            return False

        if isinstance(multiprocessing.Process, self.task):
            return self.task.is_alive()
        if isinstance(threading.Thread, self.task):
            return self.task.is_alive()
        if isinstance(asyncio.Task, self.task):
            return self.task.done()

        return False


class TaskManager(ABC):
    def __init__(self, task_done_timeout: int = 5) -> None:
        """
        just use dict to store process for local task
        !TODO: @weedge
        - if distributed task, need database to storage process info
        - shecdule task
        """
        self._tasks: Dict[str, Task] = {}
        self._task_done_timeout = task_done_timeout

    @property
    def tasks(self):
        return self._tasks

    @abstractmethod
    def run_task(self, target, name: str, tag: str, **kwargs):
        """
        - use multiprocessing to run task
        - use threading to run task
        - use asyncio create task to run
        """

    def get_task_num(self, tag: str):
        num = 0
        for val in self._tasks.values():
            task: Task = val[0]
            if val.tag == tag and task.is_alive():
                num += 1
        return num

    def get_task(self, tid):
        if tid in self._tasks:
            return self._tasks[tid]
        return None

    @abstractmethod
    async def cleanup(self):
        """
        clean up process
        """
