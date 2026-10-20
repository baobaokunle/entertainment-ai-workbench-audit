"""Local runtime boundary. Phase A never starts or contacts a model."""
from .common import AppError
def generate(*args, **kwargs):
    raise AppError(503,'model_not_enabled','本地模型尚未启用；消息保存和原生聊天可继续')
