"""理想眼镜 (Livis) 平台插件负载。

这个目录会被安装器原样复制到 ``~/.hermes/plugins/livis-platform/``，因此它
**必须自包含**：内部只用相对导入，不依赖外层发行包 ``hermes_livis`` 可导入。
"""

from .adapter import register

__all__ = ["register"]
