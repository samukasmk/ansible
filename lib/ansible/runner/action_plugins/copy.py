# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
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

import os

from ansible import utils
from ansible import errors
from ansible.runner.return_data import ReturnData
import base64
import stat
import tempfile

class ActionModule(object):

    def __init__(self, runner):
        self.runner = runner

    def run(self, conn, tmp, module_name, module_args, inject, complex_args=None, **kwargs):
        ''' handler for file transfer operations '''

        # load up options
        options = {}
        if complex_args:
            options.update(complex_args)
        options.update(utils.parse_kv(module_args))
        source  = options.get('src', None)
        content = options.get('content', None)
        dest    = options.get('dest', None)

        if (source is None and content is None and not 'first_available_file' in inject) or dest is None:
            result=dict(failed=True, msg="src (or content) and dest are required")
            return ReturnData(conn=conn, result=result)
        elif (source is not None or 'first_available_file' in inject) and content is not None:
            result=dict(failed=True, msg="src and content are mutually exclusive")
            return ReturnData(conn=conn, result=result)

        # if we have first_available_file in our vars
        # look up the files and use the first one we find as src
        if 'first_available_file' in inject:
            found = False
            for fn in inject.get('first_available_file'):
                fn = utils.template(self.runner.basedir, fn, inject)
                fn = utils.path_dwim(self.runner.basedir, fn)
                if os.path.exists(fn):
                    source = fn
                    found = True
                    break
            if not found:
                results=dict(failed=True, msg="could not find src in first_available_file list")
                return ReturnData(conn=conn, result=results)
        elif content is not None:
            fd, tmp_content = tempfile.mkstemp()
            f = os.fdopen(fd, 'w')
            try:
                f.write(content)
            except Exception, err:
                os.remove(tmp_content)
                result = dict(failed=True, msg="could not write content temp file: %s" % err)
                return ReturnData(conn=conn, result=result)
            f.close()
            source = tmp_content
        else:
            source = utils.template(self.runner.basedir, source, inject)
            source = utils.path_dwim(self.runner.basedir, source)

        local_md5 = utils.md5(source)
        if local_md5 is None:
            result=dict(failed=True, msg="could not find src=%s" % source)
            return ReturnData(conn=conn, result=result)

        if dest.endswith("/"):
            base = os.path.basename(source)
            dest = os.path.join(dest, base)

        remote_md5 = self.runner._remote_md5(conn, tmp, dest)
        if remote_md5 == '3':
            # Destination is a directory
            if content is not None:
                os.remove(tmp_content)
                result = dict(failed=True, msg="can not use content with a dir as dest")
                return ReturnData(conn=conn, result=result)
            dest = os.path.join(dest, os.path.basename(source))
            remote_md5 = self.runner._remote_md5(conn, tmp, dest)

        exec_rc = None
        if local_md5 != remote_md5:

            if self.runner.diff:
                diff = self._get_diff_data(conn, tmp, inject, dest, source)
            else:
                diff = {}

            if self.runner.check:
                if content is not None:
                    os.remove(tmp_content)
                return ReturnData(conn=conn, result=dict(changed=True), diff=diff)

            # transfer the file to a remote tmp location
            tmp_src = tmp + os.path.basename(source)
            conn.put_file(source, tmp_src)
            if content is not None:
                os.remove(tmp_content)
            # fix file permissions when the copy is done as a different user
            if self.runner.sudo and self.runner.sudo_user != 'root':
                self.runner._low_level_exec_command(conn, "chmod a+r %s" % tmp_src, tmp)

            # run the copy module
            module_args = "%s src=%s" % (module_args, tmp_src)
            return self.runner._execute_module(conn, tmp, 'copy', module_args, inject=inject, complex_args=complex_args)

        else:
            # no need to transfer the file, already correct md5, but still need to call
            # the file module in case we want to change attributes

            if content is not None:
                os.remove(tmp_content)
            tmp_src = tmp + os.path.basename(source)
            module_args = "%s src=%s" % (module_args, tmp_src)
            if self.runner.check:
                module_args = "%s CHECKMODE=True" % module_args
            return self.runner._execute_module(conn, tmp, 'file', module_args, inject=inject, complex_args=complex_args)

    def _get_diff_data(self, conn, tmp, inject, destination, source):
        peek_result = self.runner._execute_module(conn, tmp, 'file', "path=%s diff_peek=1" % destination, inject=inject, persist_files=True)

        if not peek_result.is_successful():
            return {}

        diff = {}
        if peek_result.result['state'] == 'absent':
            diff['before'] = ''
        elif peek_result.result['appears_binary']:
            diff['dst_binary'] = 1
        elif peek_result.result['size'] > utils.MAX_FILE_SIZE_FOR_DIFF:
            diff['dst_larger'] = utils.MAX_FILE_SIZE_FOR_DIFF
        else:
            dest_result = self.runner._execute_module(conn, tmp, 'slurp', "path=%s" % destination, inject=inject, persist_files=True)
            if 'content' in dest_result.result:
                dest_contents = dest_result.result['content']
                if dest_result.result['encoding'] == 'base64':
                    dest_contents = base64.b64decode(dest_contents)
                else:
                    raise Exception("unknown encoding, failed: %s" % dest_result.result)
                diff['before_header'] = destination
                diff['before'] = dest_contents

        src = open(source)
        src_contents = src.read(8192)
        st = os.stat(source)
        if src_contents.find("\x00") != -1:
            diff['src_binary'] = 1
        elif st[stat.ST_SIZE] > utils.MAX_FILE_SIZE_FOR_DIFF:
            diff['src_larger'] = utils.MAX_FILE_SIZE_FOR_DIFF
        else:
            src.seek(0)
            diff['after_header'] = source
            diff['after'] = src.read()

        return diff
