"""
ResourceEnvelope — T/Q 资源实体与库存 slot 的生命周期绑定

slot 获取与 envelope 创建必须通过 create_with_slot() 完成。
release_slot_once() 幂等:重复调用只释放一次。
"""
import time
import asyncio


class ResourceEnvelope:
    """绑定一个 T/Q 资源值与其对应的库存 slot。"""

    __slots__ = ('kind', 'value', 'slot_sem', '_released', 'created_at', 'expires_at', 'meta')

    def __init__(self, kind, value, slot_sem, *, created_at, expires_at=None, meta=None):
        self.kind = kind          # 'T' or 'Q'
        self.value = value        # T: turnstile token str; Q: {'email','password','code'}
        self.slot_sem = slot_sem  # T_Slot_Sem or Q_Slot_Sem
        self._released = False
        self.created_at = created_at
        self.expires_at = expires_at
        self.meta = meta or {}

    @classmethod
    async def create_with_slot(cls, kind, value, slot_sem, *, expires_at=None, meta=None):
        """先获取库存 slot,再构造 envelope。slot 获取失败则抛异常,不创建 envelope。"""
        await slot_sem.acquire()
        try:
            return cls(
                kind, value, slot_sem,
                created_at=time.time(),
                expires_at=expires_at,
                meta=meta,
            )
        except BaseException:
            slot_sem.release()
            raise

    def release_slot_once(self, reason=''):
        """幂等释放 slot。重复调用安全。"""
        if not self._released:
            self._released = True
            self.slot_sem.release()

    def discard(self, reason=''):
        """资源未被消费,释放库存占用。"""
        self.release_slot_once(reason=f'discard:{reason}')

    def is_expired(self, now=None):
        """检查资源是否过期。"""
        if self.expires_at is None:
            return False
        return (now or time.time()) > self.expires_at

    @property
    def released(self):
        return self._released

    def __repr__(self):
        return f'Envelope({self.kind}, released={self._released})'
