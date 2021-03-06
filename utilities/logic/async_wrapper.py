#!/usr/bin/python
# -*- coding: utf-8 -*-

# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>, and others
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
#

try:
    import json
except ImportError:
    import simplejson as json
import shlex
import os
import subprocess
import sys
import traceback
import signal
import time
import syslog


syslog.openlog('ansible-%s' % os.path.basename(__file__))
syslog.syslog(syslog.LOG_NOTICE, 'Invoked with %s' % " ".join(sys.argv[1:]))

def notice(msg):
    syslog.syslog(syslog.LOG_NOTICE, msg)

def daemonize_self():
    # daemonizing code: http://aspn.activestate.com/ASPN/Cookbook/Python/Recipe/66012
    try:
        pid = os.fork()
        if pid > 0:
            # exit first parent
            sys.exit(0)
    except OSError, e:
        print >>sys.stderr, "fork #1 failed: %d (%s)" % (e.errno, e.strerror)
        sys.exit(1)

    # decouple from parent environment
    os.chdir("/")
    os.setsid()
    os.umask(022)

    # do second fork
    try:
        pid = os.fork()
        if pid > 0:
            # print "Daemon PID %d" % pid
            sys.exit(0)
    except OSError, e:
        print >>sys.stderr, "fork #2 failed: %d (%s)" % (e.errno, e.strerror)
        sys.exit(1)

    dev_null = file('/dev/null','rw')
    os.dup2(dev_null.fileno(), sys.stdin.fileno())
    os.dup2(dev_null.fileno(), sys.stdout.fileno())
    os.dup2(dev_null.fileno(), sys.stderr.fileno())


def _run_module(wrapped_cmd, jid, job_path):

    jobfile = open(job_path, "w")
    jobfile.write(json.dumps({ "started" : 1, "ansible_job_id" : jid }))
    jobfile.close()
    jobfile = open(job_path, "w")
    result = {}

    outdata = ''
    try:
        cmd = shlex.split(wrapped_cmd)
        script = subprocess.Popen(cmd, shell=False, stdin=None, stdout=jobfile, stderr=jobfile)
        script.communicate()
        outdata = file(job_path).read()
        result = json.loads(outdata)

    except (OSError, IOError), e:
        result = {
            "failed": 1,
            "cmd" : wrapped_cmd,
            "msg": str(e),
        }
        result['ansible_job_id'] = jid
        jobfile.write(json.dumps(result))
    except:
        result = {
            "failed" : 1,
            "cmd" : wrapped_cmd,
            "data" : outdata, # temporary notice only
            "msg" : traceback.format_exc()
        }
        result['ansible_job_id'] = jid
        jobfile.write(json.dumps(result))
    jobfile.close()


####################
##      main      ##
####################
if __name__ == '__main__':

    if len(sys.argv) < 3:
        print json.dumps({
            "failed" : True,
            "msg"    : "usage: async_wrapper <jid> <time_limit> <modulescript> <argsfile>.  Humans, do not call directly!"
        })
        sys.exit(1)

    jid = "%s.%d" % (sys.argv[1], os.getpid())
    time_limit = sys.argv[2]
    wrapped_module = sys.argv[3]
    if len(sys.argv) >= 5:
        argsfile = sys.argv[4]
        cmd = "%s %s" % (wrapped_module, argsfile)
    else:
        cmd = wrapped_module
    step = 5

    # setup job output directory
    jobdir = os.path.expanduser("~/.ansible_async")
    job_path = os.path.join(jobdir, jid)

    if not os.path.exists(jobdir):
        try:
            os.makedirs(jobdir)
        except:
            print json.dumps({
                "failed" : 1,
                "msg" : "could not create: %s" % jobdir
            })
    # immediately exit this process, leaving an orphaned process
    # running which immediately forks a supervisory timing process

    try:
        pid = os.fork()
        if pid:
            # Notify the overlord that the async process started

            # we need to not return immmediately such that the launched command has an attempt
            # to initialize PRIOR to ansible trying to clean up the launch directory (and argsfile)
            # this probably could be done with some IPC later.  Modules should always read
            # the argsfile at the very first start of their execution anyway
            notice("Return async_wrapper task started.")
            print json.dumps({ "started" : 1, "ansible_job_id" : jid, "results_file" : job_path })
            sys.stdout.flush()
            time.sleep(1)
            sys.exit(0)
        else:
            # The actual wrapper process

            # Daemonize, so we keep on running
            daemonize_self()

            # we are now daemonized, create a supervisory process
            notice("Starting module and watcher")

            sub_pid = os.fork()
            if sub_pid:
                # the parent stops the process after the time limit
                remaining = int(time_limit)

                # set the child process group id to kill all children
                os.setpgid(sub_pid, sub_pid)

                notice("Start watching %s (%s)"%(sub_pid, remaining))
                time.sleep(step)
                while os.waitpid(sub_pid, os.WNOHANG) == (0, 0):
                    notice("%s still running (%s)"%(sub_pid, remaining))
                    time.sleep(step)
                    remaining = remaining - step
                    if remaining <= 0:
                        notice("Now killing %s"%(sub_pid))
                        os.killpg(sub_pid, signal.SIGKILL)
                        notice("Sent kill to group %s"%sub_pid)
                        time.sleep(1)
                        sys.exit(0)
                notice("Done in kid B.")
                sys.exit(0)
            else:
                # the child process runs the actual module
                notice("Start module (%s)"%os.getpid())
                _run_module(cmd, jid, job_path)
                notice("Module complete (%s)"%os.getpid())
                sys.exit(0)

    except Exception, err:
        notice("error: %s"%(err))
        print json.dumps({
            "failed" : True,
            "msg"    : "FATAL ERROR: %s" % str(err)
        })
        sys.exit(1)
