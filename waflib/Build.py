#!/usr/bin/env python
# encoding: utf-8
# Thomas Nagy, 2005-2010 (ita)

"""
Classes related to the build phase (build, clean, install, step, etc)

The inheritance tree is the following:

"""

import os, sys, errno, re, shutil, stat
try:
	import cPickle
except ImportError:
	import pickle as cPickle
from waflib import Runner, TaskGen, Utils, ConfigSet, Task, Logs, Options, Context, Errors
import waflib.Node

CACHE_DIR = 'c4che'
"""Location of the cache files"""

CACHE_SUFFIX = '_cache.py'
"""Suffix for the cache files"""

INSTALL = 1337
"""Positive value '->' install, see :py:attr:`waflib.Build.BuildContext.is_install`"""

UNINSTALL = -1337
"""Negative value '<-' uninstall, see :py:attr:`waflib.Build.BuildContext.is_install`"""

SAVED_ATTRS = 'root node_sigs task_sigs imp_sigs raw_deps node_deps'.split()
"""Build class members to save between the runs; these should be all dicts
except for `root` which represents a :py:class:`waflib.Node.Node` instance
"""

CFG_FILES = 'cfg_files'
"""Files from the build directory to hash before starting the build (``config.h`` written during the configuration)"""

POST_AT_ONCE = 0
"""Post mode: all task generators are posted before any task executed"""

POST_LAZY = 1
"""Post mode: post the task generators group after group, the tasks in the next group are created when the tasks in the previous groups are done"""

PROTOCOL = -1
if sys.platform == 'cli':
	PROTOCOL = 0

class BuildContext(Context.Context):
	'''executes the build'''

	cmd = 'build'
	variant = ''

	def __init__(self, **kw):
		super(BuildContext, self).__init__(**kw)

		self.is_install = 0
		"""Non-zero value when installing or uninstalling file"""

		self.top_dir = kw.get('top_dir', Context.top_dir)

		self.run_dir = kw.get('run_dir', Context.run_dir)

		self.post_mode = POST_LAZY
		"""post the task generators at once, group-by-group, or both (default is group-by-group)"""

		# output directory - may be set until the nodes are considered
		self.out_dir = kw.get('out_dir', Context.out_dir)

		self.cache_dir = kw.get('cache_dir')
		if not self.cache_dir:
			self.cache_dir = os.path.join(self.out_dir, CACHE_DIR)

		# map names to environments, the '' must be defined
		self.all_envs = {}

		# ======================================= #
		# cache variables

		self.node_sigs = {}
		"""Dict mapping build nodes to task identifier (uid), it indicates whether a task created a particular file (persists between builds)"""

		self.task_sigs = {}
		"""Dict mapping task identifiers (uid) to task signatures (persists between builds)"""

		self.imp_sigs = {}
		"""Dict mapping task identifiers (uid) to implicit task dependencies used for scanning targets (persists between builds)"""

		self.node_deps = {}
		"""Dict mapping task identifiers (uid) to node dependencies found by :py:meth:`waflib.Task.Task.scan` (persists between builds)"""

		self.raw_deps = {}
		"""Dict mapping task identifiers (uid) to custom data returned by :py:meth:`waflib.Task.Task.scan` (persists between builds)"""

		# list of folders that are already scanned
		# so that we do not need to stat them one more time
		self.cache_dir_contents = {}

		self.task_gen_cache_names = {}

		self.launch_dir = Context.launch_dir

		self.jobs = Options.options.jobs
		self.targets = Options.options.targets
		self.keep = Options.options.keep
		self.progress_bar = Options.options.progress_bar

		############ stuff below has not been reviewed

		# Manual dependencies.
		self.deps_man = Utils.defaultdict(list)
		"""Manual dependencies set by :py:meth:`waflib.Build.BuildContext.add_manual_dependency`"""

		# just the structure here
		self.current_group = 0
		"""
		Current build group
		"""

		self.groups = []
		"""
		List containing lists of task generators
		"""
		self.group_names = {}
		"""
		Map group names to the group lists. See :py:meth:`waflib.Build.BuildContext.add_group`
		"""

		for v in SAVED_ATTRS:
			if not hasattr(self, v):
				setattr(self, v, {})

	def get_variant_dir(self):
		"""Getter for the variant_dir attribute"""
		if not self.variant:
			return self.out_dir
		return os.path.join(self.out_dir, self.variant)
	variant_dir = property(get_variant_dir, None)

	def __call__(self, *k, **kw):
		"""
		Create a task generator and add it to the current build group. The following forms are equivalent::

			def build(bld):
				tg = bld(a=1, b=2)

			def build(bld):
				tg = bld()
				tg.a = 1
				tg.b = 2

			def build(bld):
				tg = TaskGen.task_gen(a=1, b=2)
				bld.add_to_group(tg, None)

		:param group: group name to add the task generator to
		:type group: string
		"""
		kw['bld'] = self
		ret = TaskGen.task_gen(*k, **kw)
		self.task_gen_cache_names = {} # reset the cache, each time
		self.add_to_group(ret, group=kw.get('group'))
		return ret

	def rule(self, *k, **kw):
		"""
		Wrapper for creating a task generator using the decorator notation. The following code::

			@bld.rule(target="foo")
			def _(tsk):
				print("bar")

		is equivalent to::

			def bar(tsk):
				print("bar")

			bld(
				target = "foo",
				rule = bar,
			)
		"""
		def f(rule):
			ret = self(*k, **kw)
			ret.rule = rule
			return ret
		return f

	def __copy__(self):
		"""Implemented to prevents copies of build contexts (raises an exception)"""
		raise Errors.WafError('build contexts cannot be copied')

	def load_envs(self):
		"""
		The configuration command creates files of the form ``build/c4che/NAMEcache.py``. This method
		creates a :py:class:`waflib.ConfigSet.ConfigSet` instance for each ``NAME`` by reading those
		files. The config sets are then stored in the dict :py:attr:`waflib.Build.BuildContext.allenvs`.
		"""
		node = self.root.find_node(self.cache_dir)
		if not node:
			raise Errors.WafError('The project was not configured: run "waf configure" first!')
		lst = node.ant_glob('**/*%s' % CACHE_SUFFIX, quiet=True)

		if not lst:
			raise Errors.WafError('The cache directory is empty: reconfigure the project')

		for x in lst:
			name = x.path_from(node).replace(CACHE_SUFFIX, '').replace('\\', '/')
			env = ConfigSet.ConfigSet(x.abspath())
			self.all_envs[name] = env
			for f in env[CFG_FILES]:
				newnode = self.root.find_resource(f)
				if not newnode or not newnode.exists():
					raise Errors.WafError('Missing configuration file %r, reconfigure the project!' % f)

	def init_dirs(self):
		"""
		Initialize the project directory and the build directory by creating the nodes
		:py:attr:`waflib.Build.BuildContext.srcnode` and :py:attr:`waflib.Build.BuildContext.bldnode`
		corresponding to ``top_dir`` and ``variant_dir`` respectively. The ``bldnode`` directory will be
		created if it does not exist.
		"""

		if not (os.path.isabs(self.top_dir) and os.path.isabs(self.out_dir)):
			raise Errors.WafError('The project was not configured: run "waf configure" first!')

		self.path = self.srcnode = self.root.find_dir(self.top_dir)
		self.bldnode = self.root.make_node(self.variant_dir)
		self.bldnode.mkdir()

	def execute(self):
		"""
		Restore the data from previous builds and call :py:meth:`waflib.Build.BuildContext.execute_build`. Overrides from :py:func:`waflib.Context.Context.execute`
		"""
		self.restore()
		if not self.all_envs:
			self.load_envs()

		self.execute_build()

	def execute_build(self):
		"""
		Execute the build by:

		* reading the scripts (see :py:meth:`waflib.Context.Context.recurse`)
		* calling :py:meth:`waflib.Build.BuildContext.pre_build` to call user build functions
		* calling :py:meth:`waflib.Build.BuildContext.compile` to process the tasks
		* calling :py:meth:`waflib.Build.BuildContext.post_build` to call user build functions
		"""

		Logs.info("Waf: Entering directory `%s'", self.variant_dir)
		self.recurse([self.run_dir])
		self.pre_build()

		# display the time elapsed in the progress bar
		self.timer = Utils.Timer()

		try:
			self.compile()
		finally:
			if self.progress_bar == 1 and sys.stderr.isatty():
				c = self.producer.processed or 1
				m = self.progress_line(c, c, Logs.colors.BLUE, Logs.colors.NORMAL)
				Logs.info(m, extra={'stream': sys.stderr, 'c1': Logs.colors.cursor_off, 'c2' : Logs.colors.cursor_on})
			Logs.info("Waf: Leaving directory `%s'", self.variant_dir)
		try:
			self.producer.bld = None
			del self.producer
		except AttributeError:
			pass
		self.post_build()

	def restore(self):
		"""
		Load the data from a previous run, sets the attributes listed in :py:const:`waflib.Build.SAVED_ATTRS`
		"""
		try:
			env = ConfigSet.ConfigSet(os.path.join(self.cache_dir, 'build.config.py'))
		except EnvironmentError:
			pass
		else:
			if env['version'] < Context.HEXVERSION:
				raise Errors.WafError('Version mismatch! reconfigure the project')
			for t in env['tools']:
				self.setup(**t)

		dbfn = os.path.join(self.variant_dir, Context.DBFILE)
		try:
			data = Utils.readf(dbfn, 'rb')
		except (EnvironmentError, EOFError):
			# handle missing file/empty file
			Logs.debug('build: Could not load the build cache %s (missing)', dbfn)
		else:
			try:
				waflib.Node.pickle_lock.acquire()
				waflib.Node.Nod3 = self.node_class
				try:
					data = cPickle.loads(data)
				except Exception as e:
					Logs.debug('build: Could not pickle the build cache %s: %r', dbfn, e)
				else:
					for x in SAVED_ATTRS:
						setattr(self, x, data[x])
			finally:
				waflib.Node.pickle_lock.release()

		self.init_dirs()

	def store(self):
		"""
		Store the data for next runs, sets the attributes listed in :py:const:`waflib.Build.SAVED_ATTRS`. Uses a temporary
		file to avoid problems on ctrl+c.
		"""

		data = {}
		for x in SAVED_ATTRS:
			data[x] = getattr(self, x)
		db = os.path.join(self.variant_dir, Context.DBFILE)

		try:
			waflib.Node.pickle_lock.acquire()
			waflib.Node.Nod3 = self.node_class
			x = cPickle.dumps(data, PROTOCOL)
		finally:
			waflib.Node.pickle_lock.release()

		Utils.writef(db + '.tmp', x, m='wb')

		try:
			st = os.stat(db)
			os.remove(db)
			if not Utils.is_win32: # win32 has no chown but we're paranoid
				os.chown(db + '.tmp', st.st_uid, st.st_gid)
		except (AttributeError, OSError):
			pass

		# do not use shutil.move (copy is not thread-safe)
		os.rename(db + '.tmp', db)

	def compile(self):
		"""
		Run the build by creating an instance of :py:class:`waflib.Runner.Parallel`
		The cache file is not written if the build is up to date (no task executed).
		"""
		Logs.debug('build: compile()')

		# use another object to perform the producer-consumer logic (reduce the complexity)
		self.producer = Runner.Parallel(self, self.jobs)
		self.producer.biter = self.get_build_iterator()
		try:
			self.producer.start()
		except KeyboardInterrupt:
			self.store()
			raise
		else:
			if self.producer.dirty:
				self.store()

		if self.producer.error:
			raise Errors.BuildError(self.producer.error)

	def setup(self, tool, tooldir=None, funs=None):
		"""
		Import waf tools, used to import those accessed during the configuration::

			def configure(conf):
				conf.load('glib2')

			def build(bld):
				pass # glib2 is imported implicitly

		:param tool: tool list
		:type tool: list
		:param tooldir: optional tool directory (sys.path)
		:type tooldir: list of string
		:param funs: unused variable
		"""
		if isinstance(tool, list):
			for i in tool: self.setup(i, tooldir)
			return

		module = Context.load_tool(tool, tooldir)
		if hasattr(module, "setup"): module.setup(self)

	def get_env(self):
		"""Getter for the env property"""
		try:
			return self.all_envs[self.variant]
		except KeyError:
			return self.all_envs['']
	def set_env(self, val):
		"""Setter for the env property"""
		self.all_envs[self.variant] = val

	env = property(get_env, set_env)

	def add_manual_dependency(self, path, value):
		"""
		Adds a dependency from a node object to a value::

			def build(bld):
				bld.add_manual_dependency(
					bld.path.find_resource('wscript'),
					bld.root.find_resource('/etc/fstab'))

		:param path: file path
		:type path: string or :py:class:`waflib.Node.Node`
		:param value: value to depend on
		:type value: :py:class:`waflib.Node.Node`, string, or function returning a string
		"""
		if not path:
			raise ValueError('Invalid input path %r' % path)

		if isinstance(path, waflib.Node.Node):
			node = path
		elif os.path.isabs(path):
			node = self.root.find_resource(path)
		else:
			node = self.path.find_resource(path)
		if not node:
			raise ValueError('Could not find the path %r' % path)

		if isinstance(value, list):
			self.deps_man[node].extend(value)
		else:
			self.deps_man[node].append(value)

	def launch_node(self):
		"""Returns the launch directory as a :py:class:`waflib.Node.Node` object"""
		try:
			# private cache
			return self.p_ln
		except AttributeError:
			self.p_ln = self.root.find_dir(self.launch_dir)
			return self.p_ln

	def hash_env_vars(self, env, vars_lst):
		"""
		Hash configuration set variables::

			def build(bld):
				bld.hash_env_vars(bld.env, ['CXX', 'CC'])

		This method uses an internal cache.

		:param env: Configuration Set
		:type env: :py:class:`waflib.ConfigSet.ConfigSet`
		:param vars_lst: list of variables
		:type vars_list: list of string
		"""

		if not env.table:
			env = env.parent
			if not env:
				return Utils.SIG_NIL

		idx = str(id(env)) + str(vars_lst)
		try:
			cache = self.cache_env
		except AttributeError:
			cache = self.cache_env = {}
		else:
			try:
				return self.cache_env[idx]
			except KeyError:
				pass

		lst = [env[a] for a in vars_lst]
		cache[idx] = ret = Utils.h_list(lst)
		Logs.debug('envhash: %s %r', Utils.to_hex(ret), lst)
		return ret

	def get_tgen_by_name(self, name):
		"""
		Retrieves a task generator from its name or its target name
		the name must be unique::

			def build(bld):
				tg = bld(name='foo')
				tg == bld.get_tgen_by_name('foo')
		"""
		cache = self.task_gen_cache_names
		if not cache:
			# create the index lazily
			for g in self.groups:
				for tg in g:
					try:
						cache[tg.name] = tg
					except AttributeError:
						# raised if not a task generator, which should be uncommon
						pass
		try:
			return cache[name]
		except KeyError:
			raise Errors.WafError('Could not find a task generator for the name %r' % name)

	def progress_line(self, state, total, col1, col2):
		"""
		Compute the progress bar used by ``waf -p``
		"""
		if not sys.stderr.isatty():
			return ''

		n = len(str(total))

		Utils.rot_idx += 1
		ind = Utils.rot_chr[Utils.rot_idx % 4]

		pc = (100.*state)/total
		eta = str(self.timer)
		fs = "[%%%dd/%%d][%%s%%2d%%%%%%s][%s][" % (n, ind)
		left = fs % (state, total, col1, pc, col2)
		right = '][%s%s%s]' % (col1, eta, col2)

		cols = Logs.get_term_cols() - len(left) - len(right) + 2*len(col1) + 2*len(col2)
		if cols < 7: cols = 7

		ratio = ((cols*state)//total) - 1

		bar = ('='*ratio+'>').ljust(cols)
		msg = Logs.indicator % (left, bar, right)

		return msg

	def declare_chain(self, *k, **kw):
		"""
		Wrapper for :py:func:`waflib.TaskGen.declare_chain` provided for convenience
		"""
		return TaskGen.declare_chain(*k, **kw)

	def pre_build(self):
		"""Execute user-defined methods before the build starts, see :py:meth:`waflib.Build.BuildContext.add_pre_fun`"""
		for m in getattr(self, 'pre_funs', []):
			m(self)

	def post_build(self):
		"""Executes the user-defined methods after the build is successful, see :py:meth:`waflib.Build.BuildContext.add_post_fun`"""
		for m in getattr(self, 'post_funs', []):
			m(self)

	def add_pre_fun(self, meth):
		"""
		Bind a method to execute after the scripts are read and before the build starts::

			def mycallback(bld):
				print("Hello, world!")

			def build(bld):
				bld.add_pre_fun(mycallback)
		"""
		try:
			self.pre_funs.append(meth)
		except AttributeError:
			self.pre_funs = [meth]

	def add_post_fun(self, meth):
		"""
		Bind a method to execute immediately after the build is successful::

			def call_ldconfig(bld):
				bld.exec_command('/sbin/ldconfig')

			def build(bld):
				if bld.cmd == 'install':
					bld.add_pre_fun(call_ldconfig)
		"""
		try:
			self.post_funs.append(meth)
		except AttributeError:
			self.post_funs = [meth]

	def get_group(self, x):
		"""
		Get the group x, or return the current group if x is None

		:param x: name or number or None
		:type x: string, int or None
		"""
		if not self.groups:
			self.add_group()
		if x is None:
			return self.groups[self.current_group]
		if x in self.group_names:
			return self.group_names[x]
		return self.groups[x]

	def add_to_group(self, tgen, group=None):
		"""add a task or a task generator for the build"""
		assert(isinstance(tgen, TaskGen.task_gen) or isinstance(tgen, Task.TaskBase))
		tgen.bld = self
		self.get_group(group).append(tgen)

	def get_group_name(self, g):
		"""
		Return the name of the input build group

		:param g: build group object or build group index
		:type g: integer or list
		:return: name
		:rtype: string
		"""
		if not isinstance(g, list):
			g = self.groups[g]
		for x in self.group_names:
			if id(self.group_names[x]) == id(g):
				return x
		return ''

	def get_group_idx(self, tg):
		"""
		Index of the group containing the task generator given as argument::

			def build(bld):
				tg = bld(name='nada')
				0 == bld.get_group_idx(tg)

		:param tg: Task generator object
		:type tg: :py:class:`waflib.TaskGen.task_gen`
		"""
		se = id(tg)
		for i, tmp in enumerate(self.groups):
			for t in tmp:
				if id(t) == se:
					return i
		return None

	def add_group(self, name=None, move=True):
		"""
		Add a new group of tasks/task generators. By default the new group becomes
		the default group for new task generators. Make sure to create build groups in order.

		:param name: name for this group
		:type name: string
		:param move: set the group created as default group (True by default)
		:type move: bool
		"""
		if name and name in self.group_names:
			Logs.error('add_group: name %s already present', name)
		g = []
		self.group_names[name] = g
		self.groups.append(g)
		if move:
			self.current_group = len(self.groups) - 1

	def set_group(self, idx):
		"""
		Set the current group to be idx: now new task generators will be added to this group by default::

			def build(bld):
				bld(rule='touch ${TGT}', target='foo.txt')
				bld.add_group() # now the current group is 1
				bld(rule='touch ${TGT}', target='bar.txt')
				bld.set_group(0) # now the current group is 0
				bld(rule='touch ${TGT}', target='truc.txt') # build truc.txt before bar.txt

		:param idx: group name or group index
		:type idx: string or int
		"""
		if isinstance(idx, str):
			g = self.group_names[idx]
			for i, tmp in enumerate(self.groups):
				if id(g) == id(tmp):
					self.current_group = i
					break
		else:
			self.current_group = idx

	def total(self):
		"""
		Approximate task count: this value may be inaccurate if task generators are posted lazily (see :py:attr:`waflib.Build.BuildContext.post_mode`).
		The value :py:attr:`waflib.Runner.Parallel.total` is updated during the task execution.
		"""
		total = 0
		for group in self.groups:
			for tg in group:
				try:
					total += len(tg.tasks)
				except AttributeError:
					total += 1
		return total

	def get_targets(self):
		"""
		Return the task generator corresponding to the 'targets' list, used by :py:meth:`waflib.Build.BuildContext.get_build_iterator`::

			$ waf --targets=myprogram,myshlib
		"""
		to_post = []
		min_grp = 0
		for name in self.targets.split(','):
			tg = self.get_tgen_by_name(name)
			m = self.get_group_idx(tg)
			if m > min_grp:
				min_grp = m
				to_post = [tg]
			elif m == min_grp:
				to_post.append(tg)
		return (min_grp, to_post)

	def get_all_task_gen(self):
		"""
		Utility method, returns a list of all task generators - if you need something more complicated, implement your own
		"""
		lst = []
		for g in self.groups:
			lst.extend(g)
		return lst

	def post_group(self):
		"""
		Post the task generators from the group indexed by self.cur, used by :py:meth:`waflib.Build.BuildContext.get_build_iterator`
		"""
		if self.targets == '*':
			for tg in self.groups[self.cur]:
				try:
					f = tg.post
				except AttributeError:
					pass
				else:
					f()
		elif self.targets:
			if self.cur < self._min_grp:
				for tg in self.groups[self.cur]:
					try:
						f = tg.post
					except AttributeError:
						pass
					else:
						f()
			else:
				for tg in self._exact_tg:
					tg.post()
		else:
			ln = self.launch_node()
			if ln.is_child_of(self.bldnode):
				Logs.warn('Building from the build directory, forcing --targets=*')
				ln = self.srcnode
			elif not ln.is_child_of(self.srcnode):
				Logs.warn('CWD %s is not under %s, forcing --targets=* (run distclean?)', ln.abspath(), self.srcnode.abspath())
				ln = self.srcnode
			for tg in self.groups[self.cur]:
				try:
					f = tg.post
				except AttributeError:
					pass
				else:
					if tg.path.is_child_of(ln):
						f()

	def get_tasks_group(self, idx):
		"""
		Return all the tasks for the group of num idx, used by :py:meth:`waflib.Build.BuildContext.get_build_iterator`
		"""
		tasks = []
		for tg in self.groups[idx]:
			try:
				tasks.extend(tg.tasks)
			except AttributeError: # not a task generator
				tasks.append(tg)
		return tasks

	def get_build_iterator(self):
		"""
		Creates a generator object that returns lists of tasks executable in parallel (yield)

		:return: tasks which can be executed immediatly
		:rtype: list of :py:class:`waflib.Task.TaskBase`
		"""
		self.cur = 0

		if self.targets and self.targets != '*':
			(self._min_grp, self._exact_tg) = self.get_targets()

		global lazy_post
		if self.post_mode != POST_LAZY:
			while self.cur < len(self.groups):
				self.post_group()
				self.cur += 1
			self.cur = 0

		while self.cur < len(self.groups):
			# first post the task generators for the group
			if self.post_mode != POST_AT_ONCE:
				self.post_group()

			# then extract the tasks
			tasks = self.get_tasks_group(self.cur)
			# if the constraints are set properly (ext_in/ext_out, before/after)
			# the call to set_file_constraints may be removed (can be a 15% penalty on no-op rebuilds)
			# (but leave set_file_constraints for the installation step)
			#
			# if the tasks have only files, set_file_constraints is required but set_precedence_constraints is not necessary
			#
			Task.set_file_constraints(tasks)
			Task.set_precedence_constraints(tasks)

			self.cur_tasks = tasks
			self.cur += 1
			if not tasks: # return something else the build will stop
				continue
			yield tasks

		while 1:
			yield []

	def install_files(self, dest, files, **kw):
		"""
		Create a task to install files on the system::

			def build(bld):
				bld.install_files('${DATADIR}', self.path.find_resource('wscript'))

		:param dest: absolute path of the destination directory
		:type dest: string
		:param files: input files
		:type files: list of strings or list of nodes
		:param env: configuration set for performing substitutions in dest
		:type env: Configuration set
		:param relative_trick: preserve the folder hierarchy when installing whole folders
		:type relative_trick: bool
		:param cwd: parent node for searching srcfile, when srcfile is not a :py:class:`waflib.Node.Node`
		:type cwd: :py:class:`waflib.Node.Node`
		:param add: add the task created to a build group - set ``False`` only if the installation task is created after the build has started
		:type add: bool
		:param postpone: execute the task immediately to perform the installation
		:type postpone: bool
		"""
		assert(dest)
		tg = self(features='install_task', install_to=dest, install_from=files, **kw)
		tg.dest = tg.install_to
		tg.type = 'install_files'
		# TODO if add: self.add_to_group(tsk)
		if not kw.get('postpone', True):
			tg.post()
		return tg

	def install_as(self, dest, srcfile, **kw):
		"""
		Create a task to install a file on the system with a different name::

			def build(bld):
				bld.install_as('${PREFIX}/bin', 'myapp', chmod=Utils.O755)

		:param dest: absolute path of the destination file
		:type dest: string
		:param srcfile: input file
		:type srcfile: string or node
		:param cwd: parent node for searching srcfile, when srcfile is not a :py:class:`waflib.Node.Node`
		:type cwd: :py:class:`waflib.Node.Node`
		:param env: configuration set for performing substitutions in dest
		:type env: Configuration set
		:param add: add the task created to a build group - set ``False`` only if the installation task is created after the build has started
		:type add: bool
		:param postpone: execute the task immediately to perform the installation
		:type postpone: bool
		"""
		assert(dest)
		tg = self(features='install_task', install_to=dest, install_from=srcfile, **kw)
		tg.dest = tg.install_to
		tg.type = 'install_as'
		# TODO if add: self.add_to_group(tsk)
		if not kw.get('postpone', True):
			tg.post()
		return tg

	def symlink_as(self, dest, src, **kw):
		"""
		Create a task to install a symlink::

			def build(bld):
				bld.symlink_as('${PREFIX}/lib/libfoo.so', 'libfoo.so.1.2.3')

		:param dest: absolute path of the symlink
		:type dest: string
		:param src: absolute or relative path of the link
		:type src: string
		:param env: configuration set for performing substitutions in dest
		:type env: Configuration set
		:param add: add the task created to a build group - set ``False`` only if the installation task is created after the build has started
		:type add: bool
		:param postpone: execute the task immediately to perform the installation
		:type postpone: bool
		:param relative_trick: make the symlink relative (default: ``False``)
		:type relative_trick: bool
		"""
		assert(dest)
		tg = self(features='install_task', install_to=dest, install_from=src, **kw)
		tg.dest = tg.install_to
		tg.type = 'symlink_as'
		tg.link = src
		# TODO if add: self.add_to_group(tsk)
		if not kw.get('postpone', True):
			tg.post()
		return tg

@TaskGen.feature('install_task')
@TaskGen.before_method('process_rule', 'process_source')
def process_install_task(self):
	# the problem is that we want to re-use
	self.add_install_task(**self.__dict__)

@TaskGen.taskgen_method
def add_install_task(self, **kw):
	if not self.bld.is_install:
		return
	if not kw['install_to']:
		return

	if kw['type'] == 'symlink_as' and Utils.is_win32:
		if kw.get('win32_install'):
			kw['type'] = 'install_as'
		else:
			# just exit
			return

	tsk = self.install_task = self.create_task('inst')
	tsk.chmod = kw.get('chmod', Utils.O644)
	tsk.link = kw.get('link', '') or kw.get('install_from', '')
	tsk.relative_trick = kw.get('relative_trick', False)
	tsk.type = kw['type']
	tsk.install_to = tsk.dest = kw['install_to']
	tsk.install_from = kw['install_from']
	tsk.relative_base = kw.get('cwd') or kw.get('relative_base', self.path)
	tsk.init_files()
	if not kw.get('postpone', True):
		tsk.run_now()
	return tsk

@TaskGen.taskgen_method
def add_install_files(self, **kw):
	kw['type'] = 'install_files'
	return self.add_install_task(**kw)

@TaskGen.taskgen_method
def add_install_as(self, **kw):
	kw['type'] = 'install_as'
	return self.add_install_task(**kw)

@TaskGen.taskgen_method
def add_symlink_as(self, **kw):
	kw['type'] = 'symlink_as'
	return self.add_install_task(**kw)

class inst(Task.Task):
	def __str__(self):
		"""Return an empty string to disable the display"""
		return ''

	def uid(self):
		lst = self.inputs + self.outputs + [self.link, self.generator.path.abspath()]
		return Utils.h_list(lst)

	def init_files(self):
		if self.type == 'symlink_as':
			inputs = []
		else:
			inputs = self.generator.to_nodes(self.install_from)
			if self.type == 'install_as':
				assert len(inputs) == 1
		self.set_inputs(inputs)

		dest = self.get_install_path()
		outputs = []
		if self.type == 'symlink_as':
			if self.relative_trick:
				self.link = os.path.relpath(self.link, os.path.dirname(dest))
			outputs.append(self.generator.bld.root.make_node(dest))
		elif self.type == 'install_as':
			outputs.append(self.generator.bld.root.make_node(dest))
		else:
			for y in inputs:
				if self.relative_trick:
					destfile = os.path.join(dest, y.path_from(self.relative_base))
				else:
					destfile = os.path.join(dest, y.name)
				outputs.append(self.generator.bld.root.make_node(destfile))
		self.set_outputs(outputs)

	def runnable_status(self):
		"""
		Installation tasks are always executed, so this method returns either :py:const:`waflib.Task.ASK_LATER` or :py:const:`waflib.Task.RUN_ME`.
		"""
		ret = super(inst, self).runnable_status()
		if ret == Task.SKIP_ME and self.generator.bld.is_install:
			return Task.RUN_ME
		return ret

	def post_run(self):
		pass

	def get_install_path(self, destdir=True):
		dest = Utils.subst_vars(self.install_to, self.env)
		if destdir and Options.options.destdir:
			dest = os.path.join(Options.options.destdir, os.path.splitdrive(dest)[1].lstrip(os.sep))
		return dest

	def copy_fun(self, src, tgt):
		# override this if you want to strip executables
		# kw['tsk'].source is the task that created the files in the build
		if Utils.is_win32 and len(tgt) > 259 and not tgt.startswith('\\\\?\\'):
			tgt = '\\\\?\\' + tgt
		shutil.copy2(src, tgt)
		os.chmod(tgt, self.chmod)

	def rm_empty_dirs(self, tgt):
		while tgt:
			tgt = os.path.dirname(tgt)
			try:
				os.rmdir(tgt)
			except OSError:
				break

	def run(self):
		is_install = self.generator.bld.is_install
		if not is_install: # unnecessary?
			return

		for x in self.outputs:
			if is_install == INSTALL:
				x.parent.mkdir()
		if self.type == 'symlink_as':
			fun = is_install == INSTALL and self.do_link or self.do_unlink
			fun(self.link, self.outputs[0].abspath())
		else:
			fun = is_install == INSTALL and self.do_install or self.do_uninstall
			launch_node = self.generator.bld.launch_node()
			for x, y in zip(self.inputs, self.outputs):
				fun(x.abspath(), y.abspath(), x.path_from(launch_node))

	def run_now(self):
		"""Try executing the installation task right now"""
		status = self.runnable_status()
		if status not in (Task.RUN_ME, Task.SKIP_ME):
			raise Errors.TaskNotReady('Could not process %r: status %r' % (self, status))
		self.run()
		self.hasrun = Task.SUCCESS

	def do_install(self, src, tgt, lbl, **kw):
		"""
		Copy a file from src to tgt with given file permissions. The actual copy is not performed
		if the source and target file have the same size and the same timestamps. When the copy occurs,
		the file is first removed and then copied (prevent stale inodes).

		:param src: file name as absolute path
		:type src: string
		:param tgt: file destination, as absolute path
		:type tgt: string
		:param chmod: installation mode
		:type chmod: int
		"""
		if not Options.options.force:
			# check if the file is already there to avoid a copy
			try:
				st1 = os.stat(tgt)
				st2 = os.stat(src)
			except OSError:
				pass
			else:
				# same size and identical timestamps -> make no copy
				if st1.st_mtime + 2 >= st2.st_mtime and st1.st_size == st2.st_size:
					if not self.generator.bld.progress_bar:
						Logs.info('- install %s (from %s)', tgt, lbl)
					return False

		if not self.generator.bld.progress_bar:
			Logs.info('+ install %s (from %s)', tgt, lbl)

		# Give best attempt at making destination overwritable,
		# like the 'install' utility used by 'make install' does.
		try:
			os.chmod(tgt, Utils.O644 | stat.S_IMODE(os.stat(tgt).st_mode))
		except EnvironmentError:
			pass

		# following is for shared libs and stale inodes (-_-)
		try:
			os.remove(tgt)
		except OSError:
			pass

		try:
			self.copy_fun(src, tgt)
		except IOError:
			if not src.exists():
				Logs.error('File %r does not exist', src)
			raise Errors.WafError('Could not install the file %r' % tgt)

	def do_link(self, src, tgt, **kw):
		"""
		Create a symlink from tgt to src.

		:param src: file name as absolute path
		:type src: string
		:param tgt: file destination, as absolute path
		:type tgt: string
		"""
		if os.path.islink(tgt) and os.readlink(tgt) == src:
			if not self.generator.bld.progress_bar:
				Logs.info('- symlink %s (to %s)', tgt, src)
		else:
			try:
				os.remove(tgt)
			except OSError:
				pass
			if not self.generator.bld.progress_bar:
				Logs.info('+ symlink %s (to %s)', tgt, src)
			os.symlink(src, tgt)

	def do_uninstall(self, src, tgt, lbl, **kw):
		if not self.generator.bld.progress_bar:
			Logs.info('- remove %s', tgt)

		#self.uninstall.append(tgt)
		try:
			os.remove(tgt)
		except OSError as e:
			if e.errno != errno.ENOENT:
				if not getattr(self, 'uninstall_error', None):
					self.uninstall_error = True
					Logs.warn('build: some files could not be uninstalled (retry with -vv to list them)')
				if Logs.verbose > 1:
					Logs.warn('Could not remove %s (error code %r)', e.filename, e.errno)
		self.rm_empty_dirs(tgt)

	def do_unlink(self, src, tgt, **kw):
		# TODO do_uninstall with proper amount of args
		try:
			if not self.generator.bld.progress_bar:
				Logs.info('- remove %s', tgt)
			os.remove(tgt)
		except OSError:
			pass
		self.rm_empty_dirs(tgt)

class InstallContext(BuildContext):
	'''installs the targets on the system'''
	cmd = 'install'

	def __init__(self, **kw):
		super(InstallContext, self).__init__(**kw)
		#self.uninstall = []
		self.is_install = INSTALL

class UninstallContext(InstallContext):
	'''removes the targets installed'''
	cmd = 'uninstall'

	def __init__(self, **kw):
		super(UninstallContext, self).__init__(**kw)
		self.is_install = UNINSTALL

	def execute(self):
		"""
		See :py:func:`waflib.Context.Context.execute`
		"""
		# TODO just mark the tasks are already run with hasrun=Task.SKIPPED
		try:
			# do not execute any tasks
			def runnable_status(self):
				return Task.SKIP_ME
			setattr(Task.Task, 'runnable_status_back', Task.Task.runnable_status)
			setattr(Task.Task, 'runnable_status', runnable_status)

			super(UninstallContext, self).execute()
		finally:
			setattr(Task.Task, 'runnable_status', Task.Task.runnable_status_back)

class CleanContext(BuildContext):
	'''cleans the project'''
	cmd = 'clean'
	def execute(self):
		"""
		See :py:func:`waflib.Context.Context.execute`
		"""
		self.restore()
		if not self.all_envs:
			self.load_envs()

		self.recurse([self.run_dir])
		try:
			self.clean()
		finally:
			self.store()

	def clean(self):
		"""Remove files from the build directory if possible, and reset the caches"""
		Logs.debug('build: clean called')

		if self.bldnode != self.srcnode:
			# would lead to a disaster if top == out
			lst = []
			for env in self.all_envs.values():
				lst.extend(self.root.find_or_declare(f) for f in env[CFG_FILES])
			for n in self.bldnode.ant_glob('**/*', excl='.lock* *conf_check_*/** config.log c4che/*', quiet=True):
				if n in lst:
					continue
				n.delete()
		self.root.children = {}

		for v in SAVED_ATTRS:
			if v == 'root':
				continue
			setattr(self, v, {})

class ListContext(BuildContext):
	'''lists the targets to execute'''

	cmd = 'list'
	def execute(self):
		"""
		See :py:func:`waflib.Context.Context.execute`.
		"""
		self.restore()
		if not self.all_envs:
			self.load_envs()

		self.recurse([self.run_dir])
		self.pre_build()

		# display the time elapsed in the progress bar
		self.timer = Utils.Timer()

		for g in self.groups:
			for tg in g:
				try:
					f = tg.post
				except AttributeError:
					pass
				else:
					f()

		try:
			# force the cache initialization
			self.get_tgen_by_name('')
		except Errors.WafError:
			pass

		for k in sorted(self.task_gen_cache_names.keys()):
			Logs.pprint('GREEN', k)

class StepContext(BuildContext):
	'''executes tasks in a step-by-step fashion, for debugging'''
	cmd = 'step'

	def __init__(self, **kw):
		super(StepContext, self).__init__(**kw)
		self.files = Options.options.files

	def compile(self):
		"""
		Compile the tasks matching the input/output files given (regular expression matching). Derived from :py:meth:`waflib.Build.BuildContext.compile`::

			$ waf step --files=foo.c,bar.c,in:truc.c,out:bar.o
			$ waf step --files=in:foo.cpp.1.o # link task only

		"""
		if not self.files:
			Logs.warn('Add a pattern for the debug build, for example "waf step --files=main.c,app"')
			BuildContext.compile(self)
			return

		targets = []
		if self.targets and self.targets != '*':
			targets = self.targets.split(',')

		for g in self.groups:
			for tg in g:
				if targets and tg.name not in targets:
					continue

				try:
					f = tg.post
				except AttributeError:
					pass
				else:
					f()

			for pat in self.files.split(','):
				matcher = self.get_matcher(pat)
				for tg in g:
					if isinstance(tg, Task.TaskBase):
						lst = [tg]
					else:
						lst = tg.tasks
					for tsk in lst:
						do_exec = False
						for node in getattr(tsk, 'inputs', []):
							if matcher(node, output=False):
								do_exec = True
								break
						for node in getattr(tsk, 'outputs', []):
							if matcher(node, output=True):
								do_exec = True
								break
						if do_exec:
							ret = tsk.run()
							Logs.info('%s -> exit %r', tsk, ret)

	def get_matcher(self, pat):
		# this returns a function
		inn = True
		out = True
		if pat.startswith('in:'):
			out = False
			pat = pat.replace('in:', '')
		elif pat.startswith('out:'):
			inn = False
			pat = pat.replace('out:', '')

		anode = self.root.find_node(pat)
		pattern = None
		if not anode:
			if not pat.startswith('^'):
				pat = '^.+?%s' % pat
			if not pat.endswith('$'):
				pat = '%s$' % pat
			pattern = re.compile(pat)

		def match(node, output):
			if output == True and not out:
				return False
			if output == False and not inn:
				return False

			if anode:
				return anode == node
			else:
				return pattern.match(node.abspath())
		return match

class EnvContext(BuildContext):
	"""Subclass EnvContext to create commands that require configuration data in 'env'"""
	fun = cmd = None
	def execute(self):
		self.restore()
		if not self.all_envs:
			self.load_envs()
		self.recurse([self.run_dir])

