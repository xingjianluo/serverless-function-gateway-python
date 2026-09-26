"""骨架冒烟测试：仅验证包可导入、类可实例化。"""


def test_skeleton_imports():
    from gateway.invocation import Gateway
    from gateway.registry import Registry
    from gateway.executor import Executor

    g = Gateway(Registry())
    assert isinstance(g.executor, Executor)
