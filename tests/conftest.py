"""
共享 fixtures — 供四层测试共用
"""
import sys
import os
import asyncio
import pytest

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grok_register.core.inventory import Inventory
from grok_register.core.observer import Metrics
from tests.fakes import (
    FakeTurnstile, FakeEmailService, FakeRegisterAPI,
    Conservation, EventLog,
)


@pytest.fixture
def metrics():
    return Metrics()


@pytest.fixture
def inventory(metrics):
    return Inventory(metrics=metrics)


@pytest.fixture
def sems():
    """标准 Semaphore 集合(小容量,便于测试)。"""
    return {
        'physical': asyncio.Semaphore(4),
        't_slot': asyncio.Semaphore(4),
        'q_slot': asyncio.Semaphore(4),
        'q_pending': asyncio.Semaphore(6),
    }


@pytest.fixture
def large_sems():
    """大容量 Semaphore(压力测试用)。"""
    return {
        'physical': asyncio.Semaphore(32),
        't_slot': asyncio.Semaphore(64),
        'q_slot': asyncio.Semaphore(64),
        'q_pending': asyncio.Semaphore(48),
    }


@pytest.fixture
def conservation(sems, inventory):
    return Conservation(
        sems['t_slot'], sems['q_slot'], sems['q_pending'], inventory
    )


@pytest.fixture
def fake_turnstile():
    return FakeTurnstile()


@pytest.fixture
def fake_email():
    return FakeEmailService()


@pytest.fixture
def fake_register():
    return FakeRegisterAPI()


@pytest.fixture
def event_log():
    return EventLog()


@pytest.fixture
def stop():
    return asyncio.Event()


@pytest.fixture
def file_lock():
    return asyncio.Lock()


# ── 跳过需要网络的测试(本地开发时) ──
def pytest_configure(config):
    config.addinivalue_line("markers", "slow: 压力测试(较慢)")
    config.addinivalue_line("markers", "needs_network: 需要外部网络")
