# Copyright (C) 2007-2010 Samuel Abels.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2, as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
import threading
import gc
from itertools              import chain
from collections            import defaultdict, deque
from Exscript.util.event    import Event
from Exscript.workqueue.Job import Job

class MainLoop(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.job_started_event   = Event()
        self.job_error_event     = Event()
        self.job_succeeded_event = Event()
        self.job_aborted_event   = Event()
        self.queue_empty_event   = Event()
        self.queue               = deque()
        self.force_start         = []
        self.running_jobs        = []
        self.sleeping_actions    = []
        self.paused              = True
        self.shutdown_now        = False
        self.max_threads         = 1
        self.condition           = threading.Condition()
        self.debug               = 5
        self.daemon              = True

    def _dbg(self, level, msg):
        if self.debug >= level:
            print msg

    def _action_sleep_notify(self, action):
        assert self.in_progress(action)
        with self.condition:
            self.sleeping_actions.append(action)
            self.condition.notify_all()

    def _action_wake_notify(self, action):
        assert self.in_progress(action)
        assert action in self.sleeping_actions
        with self.condition:
            self.sleeping_actions.remove(action)
            self.condition.notify_all()

    def get_max_threads(self):
        return self.max_threads

    def set_max_threads(self, max_threads):
        assert max_threads is not None
        with self.condition:
            self.max_threads = int(max_threads)
            self.condition.notify_all()

    def _create_job(self, action, times):
        job = Job(self.condition, action, action.name)
        job.times = times
        return job

    def enqueue(self, action, times):
        with self.condition:
            self.queue.append(self._create_job(action, times))
            self.condition.notify_all()

    def enqueue_or_ignore(self, action, times):
        with self.condition:
            if self.get_first_job_from_name(action.name) is None:
                job = self._create_job(action, times)
                self.queue.append(job)
                enqueued = True
            else:
                enqueued = False
            self.condition.notify_all()
        return enqueued

    def priority_enqueue(self, action, force_start, times):
        with self.condition:
            job = self._create_job(action, times)
            if force_start:
                self.force_start.append(job)
            else:
                self.queue.appendleft(job)
            self.condition.notify_all()

    def priority_enqueue_or_raise(self, action, force_start, times):
        with self.condition:
            # If the action is already running (or about to be forced),
            # there is nothing to be done.
            for job in chain(self.force_start, self.running_jobs):
                if job.name == action.name:
                    self.condition.notify_all()
                    return False

            # If the action is already in the queue, remove it so it can be
            # re-added later.
            existing = None
            for job in self.queue:
                if job.name == action.name:
                    existing = job
                    break
            if existing:
                self.queue.remove(existing)
                action = existing.action

            # Now add the action to the queue.
            job = self._create_job(action, times)
            if force_start:
                self.force_start.append(job)
            else:
                self.queue.appendleft(job)

            self.condition.notify_all()
        return existing is None

    def pause(self):
        with self.condition:
            self.paused = True
            self.condition.notify_all()

    def resume(self):
        with self.condition:
            self.paused = False
            self.condition.notify_all()

    def is_paused(self):
        return self.paused

    def _get_action_from_hash(self, thehash):
        for job in chain(self.queue, self.force_start, self.running_jobs):
            if job.action.__hash__() == thehash:
                return job.action
        return None

    def wait_for(self, action):
        with self.condition:
            # If we were passed the hash of an action, look the action up
            # first.
            if isinstance(action, int):
                action = self._get_action_from_hash(action)
                if action is None:
                    return

            # Wait until the action completes.
            while self.in_queue(action):
                self.condition.wait()

    def wait_for_activity(self):
        with self.condition:
            self.condition.wait(.2)

    def wait_until_done(self):
        with self.condition:
            while self.get_queue_length() > 0:
                self.condition.wait()

    def shutdown(self):
        with self.condition:
            self.shutdown_now = True
            self.condition.notify_all()
        for job in self.running_jobs:
            job.join()
            self._dbg(1, 'Job "%s" finished' % job.name)

    def in_queue(self, action):
        jobs = chain(self.queue, self.force_start, self.running_jobs)
        return action in [j.action for j in jobs]

    def in_progress(self, action):
        return action in self.get_running_actions()

    def get_running_actions(self):
        return [job.action for job in self.running_jobs]

    def get_queue_length(self):
        return len(self.queue) \
             + len(self.force_start) \
             + len(self.running_jobs)

    def get_first_job_from_name(self, job_name):
        if job_name is None:
            return None
        for job in chain(self.queue, self.force_start, self.running_jobs):
            if job.name == job_name:
                return job
        return None

    def _start_job(self, job):
        self.running_jobs.append(job)
        self.job_started_event(job.action)
        job.start()
        self._dbg(1, 'Job "%s" started.' % job.name)

    def _restart_job(self, job):
        self._start_job(copy(job))

    def _on_job_completed(self, job):
        if job.exc_info:
            self._dbg(1, 'Error in job "%s"' % job.name)
            self.job_error_event(job.action, job.exc_info)
            if job.failures >= job.times:
                self._dbg(1, 'Job "%s" finally failed' % job.name)
                self.job_aborted_event(job.action)
            else:
                self._dbg(1, 'Restarting job "%s"' % job.name)
                self._restart_job(job)
        else:
            self._dbg(1, 'Job "%s" succeeded.' % job.getName())
            self.job_succeeded_event(job.action)

    def _update_running_jobs(self):
        # Update the list of running jobs.
        running   = []
        completed = []
        for job in self.running_jobs:
            if job.is_alive():
                running.append(job)
                continue
            completed.append(job)
        self.running_jobs = running[:]

        # Notify any clients *after* removing the job from the list.
        for job in completed:
            self._on_job_completed(job)
            job.join()
            del job
        gc.collect()

    def run(self):
        self.condition.acquire()
        while not self.shutdown_now:
            self._update_running_jobs()
            if self.get_queue_length() == 0:
                self.queue_empty_event()

            # If there are any actions to be force_started, run them now.
            for job in self.force_start:
                self._start_job(job)
            self.force_start = []
            self.condition.notify_all()

            # Don't bother looking if the queue is empty.
            if len(self.queue) <= 0 or self.paused:
                self.condition.wait()
                continue

            # Wait until we have less than the maximum number of threads.
            active = len(self.running_jobs) - len(self.sleeping_actions)
            if active >= self.max_threads:
                self.condition.wait()
                continue

            # Take the next action and start it in a new thread.
            job = self.queue.popleft()
            self._start_job(job)
            self.condition.release()

            if len(self.queue) <= 0:
                self._dbg(2, 'No more pending actions in the queue.')
            self.condition.acquire()
        self.condition.release()
        self._dbg(2, 'Main loop terminated.')
