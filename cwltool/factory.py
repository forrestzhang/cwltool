from . import main
from . import workflow
import os
from .process import Process
from typing import Any, Union
import argparse

class Callable(object):
    def __init__(self, t, factory):  # type: (Process, Factory) -> None
        self.t = t
        self.factory = factory

    def __call__(self, **kwargs):  # type: (**Any) -> Union[str,Dict[str,str]]
        return self.factory.executor(self.t, kwargs, os.getcwd(), None, **self.factory.execkwargs)

class Factory(object):
    def __init__(self, makeTool=workflow.defaultMakeTool,
                 executor=main.single_job_executor,
                 **execkwargs):
        # type: (Callable[[Process],None],Callable[[Process, Dict[str,Any], str, argparse.Namespace,Any],Union[str,Dict[str,str]]], **Any) -> None
        self.makeTool = makeTool
        self.executor = executor
        self.execkwargs = execkwargs

    def make(self, cwl, frag=None, debug=False):
        l = main.load_tool(cwl, False, True, self.makeTool, debug, urifrag=frag)
        if type(l) == int:
            raise Exception("Error loading tool")
        return Callable(l, self)
