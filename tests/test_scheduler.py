import pytest

from design_helpers import _run


class TestScheduler:
    """Scheduler lifecycle."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from companest.jobs.scheduler import Scheduler

        self.calls = 0

        async def task():
            self.calls += 1

        self.s = Scheduler()
        self.s.add("test_task", task, interval=3600)

    def test_add_and_status(self):
        status = self.s.get_status()
        assert not status["started"]
        assert "test_task" in status["tasks"]
        assert status["tasks"]["test_task"]["interval_seconds"] == 3600

    def test_start_stop(self):
        async def lifecycle():
            await self.s.start()
            assert self.s.get_status()["started"]
            assert self.s.get_status()["tasks"]["test_task"]["running"]
            await self.s.stop()
            assert not self.s.get_status()["started"]

        _run(lifecycle())

    def test_remove(self):
        assert self.s.remove("test_task")
        assert "test_task" not in self.s._tasks

    def test_run_now(self):
        _run(self.s.run_now("test_task"))
        assert self.calls == 1
