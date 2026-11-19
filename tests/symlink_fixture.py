import unittest


def create_symlink(link, target, **kwargs):
    try:
        link.symlink_to(target, **kwargs)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            raise unittest.SkipTest("当前 Windows 账户无权创建符号链接测试样例") from error
        raise
