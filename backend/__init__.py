# backend/ 必须是一个 Python 包，工具内的模块才能用相对导入（from . import x）。
# 宿主 toolbox_manager._ensure_backend_package() 靠本文件的存在与否决定注册成包还是单模块。
