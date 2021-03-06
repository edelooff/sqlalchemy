# sql/base.py
# Copyright (C) 2005-2019 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

"""Foundational utilities common to many sql modules.

"""


import itertools
import operator
import re

from .visitors import ClauseVisitor
from .. import exc
from .. import util

coercions = None  # type: types.ModuleType
elements = None  # type: types.ModuleType
type_api = None  # type: types.ModuleType

PARSE_AUTOCOMMIT = util.symbol("PARSE_AUTOCOMMIT")
NO_ARG = util.symbol("NO_ARG")


class Immutable(object):
    """mark a ClauseElement as 'immutable' when expressions are cloned."""

    def unique_params(self, *optionaldict, **kwargs):
        raise NotImplementedError("Immutable objects do not support copying")

    def params(self, *optionaldict, **kwargs):
        raise NotImplementedError("Immutable objects do not support copying")

    def _clone(self):
        return self


def _from_objects(*elements):
    return itertools.chain(*[element._from_objects for element in elements])


@util.decorator
def _generative(fn, *args, **kw):
    """Mark a method as generative."""

    self = args[0]._generate()
    fn(self, *args[1:], **kw)
    return self


def _clone(element, **kw):
    return element._clone()


def _expand_cloned(elements):
    """expand the given set of ClauseElements to be the set of all 'cloned'
    predecessors.

    """
    return itertools.chain(*[x._cloned_set for x in elements])


def _cloned_intersection(a, b):
    """return the intersection of sets a and b, counting
    any overlap between 'cloned' predecessors.

    The returned set is in terms of the entities present within 'a'.

    """
    all_overlap = set(_expand_cloned(a)).intersection(_expand_cloned(b))
    return set(
        elem for elem in a if all_overlap.intersection(elem._cloned_set)
    )


def _cloned_difference(a, b):
    all_overlap = set(_expand_cloned(a)).intersection(_expand_cloned(b))
    return set(
        elem for elem in a if not all_overlap.intersection(elem._cloned_set)
    )


class _DialectArgView(util.collections_abc.MutableMapping):
    """A dictionary view of dialect-level arguments in the form
    <dialectname>_<argument_name>.

    """

    def __init__(self, obj):
        self.obj = obj

    def _key(self, key):
        try:
            dialect, value_key = key.split("_", 1)
        except ValueError:
            raise KeyError(key)
        else:
            return dialect, value_key

    def __getitem__(self, key):
        dialect, value_key = self._key(key)

        try:
            opt = self.obj.dialect_options[dialect]
        except exc.NoSuchModuleError:
            raise KeyError(key)
        else:
            return opt[value_key]

    def __setitem__(self, key, value):
        try:
            dialect, value_key = self._key(key)
        except KeyError:
            raise exc.ArgumentError(
                "Keys must be of the form <dialectname>_<argname>"
            )
        else:
            self.obj.dialect_options[dialect][value_key] = value

    def __delitem__(self, key):
        dialect, value_key = self._key(key)
        del self.obj.dialect_options[dialect][value_key]

    def __len__(self):
        return sum(
            len(args._non_defaults)
            for args in self.obj.dialect_options.values()
        )

    def __iter__(self):
        return (
            util.safe_kwarg("%s_%s" % (dialect_name, value_name))
            for dialect_name in self.obj.dialect_options
            for value_name in self.obj.dialect_options[
                dialect_name
            ]._non_defaults
        )


class _DialectArgDict(util.collections_abc.MutableMapping):
    """A dictionary view of dialect-level arguments for a specific
    dialect.

    Maintains a separate collection of user-specified arguments
    and dialect-specified default arguments.

    """

    def __init__(self):
        self._non_defaults = {}
        self._defaults = {}

    def __len__(self):
        return len(set(self._non_defaults).union(self._defaults))

    def __iter__(self):
        return iter(set(self._non_defaults).union(self._defaults))

    def __getitem__(self, key):
        if key in self._non_defaults:
            return self._non_defaults[key]
        else:
            return self._defaults[key]

    def __setitem__(self, key, value):
        self._non_defaults[key] = value

    def __delitem__(self, key):
        del self._non_defaults[key]


class DialectKWArgs(object):
    """Establish the ability for a class to have dialect-specific arguments
    with defaults and constructor validation.

    The :class:`.DialectKWArgs` interacts with the
    :attr:`.DefaultDialect.construct_arguments` present on a dialect.

    .. seealso::

        :attr:`.DefaultDialect.construct_arguments`

    """

    @classmethod
    def argument_for(cls, dialect_name, argument_name, default):
        """Add a new kind of dialect-specific keyword argument for this class.

        E.g.::

            Index.argument_for("mydialect", "length", None)

            some_index = Index('a', 'b', mydialect_length=5)

        The :meth:`.DialectKWArgs.argument_for` method is a per-argument
        way adding extra arguments to the
        :attr:`.DefaultDialect.construct_arguments` dictionary. This
        dictionary provides a list of argument names accepted by various
        schema-level constructs on behalf of a dialect.

        New dialects should typically specify this dictionary all at once as a
        data member of the dialect class.  The use case for ad-hoc addition of
        argument names is typically for end-user code that is also using
        a custom compilation scheme which consumes the additional arguments.

        :param dialect_name: name of a dialect.  The dialect must be
         locatable, else a :class:`.NoSuchModuleError` is raised.   The
         dialect must also include an existing
         :attr:`.DefaultDialect.construct_arguments` collection, indicating
         that it participates in the keyword-argument validation and default
         system, else :class:`.ArgumentError` is raised.  If the dialect does
         not include this collection, then any keyword argument can be
         specified on behalf of this dialect already.  All dialects packaged
         within SQLAlchemy include this collection, however for third party
         dialects, support may vary.

        :param argument_name: name of the parameter.

        :param default: default value of the parameter.

        .. versionadded:: 0.9.4

        """

        construct_arg_dictionary = DialectKWArgs._kw_registry[dialect_name]
        if construct_arg_dictionary is None:
            raise exc.ArgumentError(
                "Dialect '%s' does have keyword-argument "
                "validation and defaults enabled configured" % dialect_name
            )
        if cls not in construct_arg_dictionary:
            construct_arg_dictionary[cls] = {}
        construct_arg_dictionary[cls][argument_name] = default

    @util.memoized_property
    def dialect_kwargs(self):
        """A collection of keyword arguments specified as dialect-specific
        options to this construct.

        The arguments are present here in their original ``<dialect>_<kwarg>``
        format.  Only arguments that were actually passed are included;
        unlike the :attr:`.DialectKWArgs.dialect_options` collection, which
        contains all options known by this dialect including defaults.

        The collection is also writable; keys are accepted of the
        form ``<dialect>_<kwarg>`` where the value will be assembled
        into the list of options.

        .. versionadded:: 0.9.2

        .. versionchanged:: 0.9.4 The :attr:`.DialectKWArgs.dialect_kwargs`
           collection is now writable.

        .. seealso::

            :attr:`.DialectKWArgs.dialect_options` - nested dictionary form

        """
        return _DialectArgView(self)

    @property
    def kwargs(self):
        """A synonym for :attr:`.DialectKWArgs.dialect_kwargs`."""
        return self.dialect_kwargs

    @util.dependencies("sqlalchemy.dialects")
    def _kw_reg_for_dialect(dialects, dialect_name):
        dialect_cls = dialects.registry.load(dialect_name)
        if dialect_cls.construct_arguments is None:
            return None
        return dict(dialect_cls.construct_arguments)

    _kw_registry = util.PopulateDict(_kw_reg_for_dialect)

    def _kw_reg_for_dialect_cls(self, dialect_name):
        construct_arg_dictionary = DialectKWArgs._kw_registry[dialect_name]
        d = _DialectArgDict()

        if construct_arg_dictionary is None:
            d._defaults.update({"*": None})
        else:
            for cls in reversed(self.__class__.__mro__):
                if cls in construct_arg_dictionary:
                    d._defaults.update(construct_arg_dictionary[cls])
        return d

    @util.memoized_property
    def dialect_options(self):
        """A collection of keyword arguments specified as dialect-specific
        options to this construct.

        This is a two-level nested registry, keyed to ``<dialect_name>``
        and ``<argument_name>``.  For example, the ``postgresql_where``
        argument would be locatable as::

            arg = my_object.dialect_options['postgresql']['where']

        .. versionadded:: 0.9.2

        .. seealso::

            :attr:`.DialectKWArgs.dialect_kwargs` - flat dictionary form

        """

        return util.PopulateDict(
            util.portable_instancemethod(self._kw_reg_for_dialect_cls)
        )

    def _validate_dialect_kwargs(self, kwargs):
        # validate remaining kwargs that they all specify DB prefixes

        if not kwargs:
            return

        for k in kwargs:
            m = re.match("^(.+?)_(.+)$", k)
            if not m:
                raise TypeError(
                    "Additional arguments should be "
                    "named <dialectname>_<argument>, got '%s'" % k
                )
            dialect_name, arg_name = m.group(1, 2)

            try:
                construct_arg_dictionary = self.dialect_options[dialect_name]
            except exc.NoSuchModuleError:
                util.warn(
                    "Can't validate argument %r; can't "
                    "locate any SQLAlchemy dialect named %r"
                    % (k, dialect_name)
                )
                self.dialect_options[dialect_name] = d = _DialectArgDict()
                d._defaults.update({"*": None})
                d._non_defaults[arg_name] = kwargs[k]
            else:
                if (
                    "*" not in construct_arg_dictionary
                    and arg_name not in construct_arg_dictionary
                ):
                    raise exc.ArgumentError(
                        "Argument %r is not accepted by "
                        "dialect %r on behalf of %r"
                        % (k, dialect_name, self.__class__)
                    )
                else:
                    construct_arg_dictionary[arg_name] = kwargs[k]


class Generative(object):
    """Allow a ClauseElement to generate itself via the
    @_generative decorator.

    """

    def _generate(self):
        s = self.__class__.__new__(self.__class__)
        s.__dict__ = self.__dict__.copy()
        return s


class Executable(Generative):
    """Mark a ClauseElement as supporting execution.

    :class:`.Executable` is a superclass for all "statement" types
    of objects, including :func:`select`, :func:`delete`, :func:`update`,
    :func:`insert`, :func:`text`.

    """

    supports_execution = True
    _execution_options = util.immutabledict()
    _bind = None

    @_generative
    def execution_options(self, **kw):
        """ Set non-SQL options for the statement which take effect during
        execution.

        Execution options can be set on a per-statement or
        per :class:`.Connection` basis.   Additionally, the
        :class:`.Engine` and ORM :class:`~.orm.query.Query` objects provide
        access to execution options which they in turn configure upon
        connections.

        The :meth:`execution_options` method is generative.  A new
        instance of this statement is returned that contains the options::

            statement = select([table.c.x, table.c.y])
            statement = statement.execution_options(autocommit=True)

        Note that only a subset of possible execution options can be applied
        to a statement - these include "autocommit" and "stream_results",
        but not "isolation_level" or "compiled_cache".
        See :meth:`.Connection.execution_options` for a full list of
        possible options.

        .. seealso::

            :meth:`.Connection.execution_options`

            :meth:`.Query.execution_options`

            :meth:`.Executable.get_execution_options`

        """
        if "isolation_level" in kw:
            raise exc.ArgumentError(
                "'isolation_level' execution option may only be specified "
                "on Connection.execution_options(), or "
                "per-engine using the isolation_level "
                "argument to create_engine()."
            )
        if "compiled_cache" in kw:
            raise exc.ArgumentError(
                "'compiled_cache' execution option may only be specified "
                "on Connection.execution_options(), not per statement."
            )
        self._execution_options = self._execution_options.union(kw)

    def get_execution_options(self):
        """ Get the non-SQL options which will take effect during execution.

        .. versionadded:: 1.3

        .. seealso::

            :meth:`.Executable.execution_options`
        """
        return self._execution_options

    def execute(self, *multiparams, **params):
        """Compile and execute this :class:`.Executable`."""
        e = self.bind
        if e is None:
            label = getattr(self, "description", self.__class__.__name__)
            msg = (
                "This %s is not directly bound to a Connection or Engine. "
                "Use the .execute() method of a Connection or Engine "
                "to execute this construct." % label
            )
            raise exc.UnboundExecutionError(msg)
        return e._execute_clauseelement(self, multiparams, params)

    def scalar(self, *multiparams, **params):
        """Compile and execute this :class:`.Executable`, returning the
        result's scalar representation.

        """
        return self.execute(*multiparams, **params).scalar()

    @property
    def bind(self):
        """Returns the :class:`.Engine` or :class:`.Connection` to
        which this :class:`.Executable` is bound, or None if none found.

        This is a traversal which checks locally, then
        checks among the "from" clauses of associated objects
        until a bound engine or connection is found.

        """
        if self._bind is not None:
            return self._bind

        for f in _from_objects(self):
            if f is self:
                continue
            engine = f.bind
            if engine is not None:
                return engine
        else:
            return None


class SchemaEventTarget(object):
    """Base class for elements that are the targets of :class:`.DDLEvents`
    events.

    This includes :class:`.SchemaItem` as well as :class:`.SchemaType`.

    """

    def _set_parent(self, parent):
        """Associate with this SchemaEvent's parent object."""

    def _set_parent_with_dispatch(self, parent):
        self.dispatch.before_parent_attach(self, parent)
        self._set_parent(parent)
        self.dispatch.after_parent_attach(self, parent)


class SchemaVisitor(ClauseVisitor):
    """Define the visiting for ``SchemaItem`` objects."""

    __traverse_options__ = {"schema_visitor": True}


class ColumnCollection(object):
    """Collection of :class:`.ColumnElement` instances, typically for
    selectables.

    The :class:`.ColumnCollection` has both mapping- and sequence- like
    behaviors.   A :class:`.ColumnCollection` usually stores :class:`.Column`
    objects, which are then accessible both via mapping style access as well
    as attribute access style.  The name for which a :class:`.Column` would
    be present is normally that of the :paramref:`.Column.key` parameter,
    however depending on the context, it may be stored under a special label
    name::

        >>> from sqlalchemy import Column, Integer
        >>> from sqlalchemy.sql import ColumnCollection
        >>> x, y = Column('x', Integer), Column('y', Integer)
        >>> cc = ColumnCollection(columns=[x, y])
        >>> cc.x
        Column('x', Integer(), table=None)
        >>> cc.y
        Column('y', Integer(), table=None)
        >>> cc['x']
        Column('x', Integer(), table=None)
        >>> cc['y']

    :class`.ColumnCollection` also indexes the columns in order and allows
    them to be accessible by their integer position::

        >>> cc[0]
        Column('x', Integer(), table=None)
        >>> cc[1]
        Column('y', Integer(), table=None)

    .. versionadded:: 1.4 :class:`.ColumnCollection` allows integer-based
       index access to the collection.

    Iterating the collection yields the column expressions in order::

        >>> list(cc)
        [Column('x', Integer(), table=None),
         Column('y', Integer(), table=None)]

    The base :class:`.ColumnCollection` object can store duplicates, which can
    mean either two columns with the same key, in which case the column
    returned by key  access is **arbitrary**::

        >>> x1, x2 = Column('x', Integer), Column('x', Integer)
        >>> cc = ColumnCollection(columns=[x1, x2])
        >>> list(cc)
        [Column('x', Integer(), table=None),
         Column('x', Integer(), table=None)]
        >>> cc['x'] is x1
        False
        >>> cc['x'] is x2
        True

    Or it can also mean the same column multiple times.   These cases are
    supported as :class:`.ColumnCollection` is used to represent the columns in
    a SELECT statement which may include duplicates.

    A special subclass :class:`.DedupeColumnCollection` exists which instead
    maintains SQLAlchemy's older behavior of not allowing duplicates; this
    collection is used for schema level objects like :class:`.Table` and
    :class:`.PrimaryKeyConstraint` where this deduping is helpful.  The
    :class:`.DedupeColumnCollection` class also has additional mutation methods
    as the schema constructs have more use cases that require removal and
    replacement of columns.

    .. versionchanged:: 1.4 :class:`.ColumnCollection` now stores duplicate
       column keys as well as the same column in multiple positions.  The
       :class:`.DedupeColumnCollection` class is added to maintain the
       former behavior in those cases where deduplication as well as
       additional replace/remove operations are needed.


    """

    __slots__ = "_collection", "_index", "_colset"

    def __init__(self, columns=None):
        object.__setattr__(self, "_colset", set())
        object.__setattr__(self, "_index", {})
        object.__setattr__(self, "_collection", [])
        if columns:
            self._initial_populate(columns)

    def _initial_populate(self, iter_):
        self._populate_separate_keys(iter_)

    @property
    def _all_columns(self):
        return [col for (k, col) in self._collection]

    def keys(self):
        return [k for (k, col) in self._collection]

    def __len__(self):
        return len(self._collection)

    def __iter__(self):
        # turn to a list first to maintain over a course of changes
        return iter([col for k, col in self._collection])

    def __getitem__(self, key):
        try:
            return self._index[key]
        except KeyError:
            if isinstance(key, util.int_types):
                raise IndexError(key)
            else:
                raise

    def __getattr__(self, key):
        try:
            return self._index[key]
        except KeyError:
            raise AttributeError(key)

    def __contains__(self, key):
        if key not in self._index:
            if not isinstance(key, util.string_types):
                raise exc.ArgumentError(
                    "__contains__ requires a string argument"
                )
            return False
        else:
            return True

    def compare(self, other):
        for l, r in util.zip_longest(self, other):
            if l is not r:
                return False
        else:
            return True

    def __eq__(self, other):
        return self.compare(other)

    def get(self, key, default=None):
        if key in self._index:
            return self._index[key]
        else:
            return default

    def __str__(self):
        return repr([str(c) for c in self])

    def __setitem__(self, key, value):
        raise NotImplementedError()

    def __delitem__(self, key):
        raise NotImplementedError()

    def __setattr__(self, key, obj):
        raise NotImplementedError()

    def clear(self):
        raise NotImplementedError()

    def remove(self, column):
        raise NotImplementedError()

    def update(self, iter_):
        raise NotImplementedError()

    __hash__ = None

    def _populate_separate_keys(self, iter_):
        """populate from an iterator of (key, column)"""
        cols = list(iter_)
        self._collection[:] = cols
        self._colset.update(c for k, c in self._collection)
        self._index.update(
            (idx, c) for idx, (k, c) in enumerate(self._collection)
        )
        self._index.update({k: col for k, col in reversed(self._collection)})

    def add(self, column, key=None):
        if key is None:
            key = column.key

        l = len(self._collection)
        self._collection.append((key, column))
        self._colset.add(column)
        self._index[l] = column
        if key not in self._index:
            self._index[key] = column

    def __getstate__(self):
        return {"_collection": self._collection, "_index": self._index}

    def __setstate__(self, state):
        object.__setattr__(self, "_index", state["_index"])
        object.__setattr__(self, "_collection", state["_collection"])
        object.__setattr__(
            self, "_colset", {col for k, col in self._collection}
        )

    def contains_column(self, col):
        return col in self._colset

    def as_immutable(self):
        return ImmutableColumnCollection(self)

    def corresponding_column(self, column, require_embedded=False):
        """Given a :class:`.ColumnElement`, return the exported
        :class:`.ColumnElement` object from this :class:`.ColumnCollection`
        which corresponds to that original :class:`.ColumnElement` via a common
        ancestor column.

        :param column: the target :class:`.ColumnElement` to be matched

        :param require_embedded: only return corresponding columns for
         the given :class:`.ColumnElement`, if the given
         :class:`.ColumnElement` is actually present within a sub-element
         of this :class:`.Selectable`.  Normally the column will match if
         it merely shares a common ancestor with one of the exported
         columns of this :class:`.Selectable`.

        .. seealso::

            :meth:`.Selectable.corresponding_column` - invokes this method
            against the collection returned by
            :attr:`.Selectable.exported_columns`.

        .. versionchanged:: 1.4 the implementation for ``corresponding_column``
           was moved onto the :class:`.ColumnCollection` itself.

        """

        def embedded(expanded_proxy_set, target_set):
            for t in target_set.difference(expanded_proxy_set):
                if not set(_expand_cloned([t])).intersection(
                    expanded_proxy_set
                ):
                    return False
            return True

        # don't dig around if the column is locally present
        if column in self._colset:
            return column
        col, intersect = None, None
        target_set = column.proxy_set
        cols = [c for (k, c) in self._collection]
        for c in cols:
            expanded_proxy_set = set(_expand_cloned(c.proxy_set))
            i = target_set.intersection(expanded_proxy_set)
            if i and (
                not require_embedded
                or embedded(expanded_proxy_set, target_set)
            ):
                if col is None:

                    # no corresponding column yet, pick this one.

                    col, intersect = c, i
                elif len(i) > len(intersect):

                    # 'c' has a larger field of correspondence than
                    # 'col'. i.e. selectable.c.a1_x->a1.c.x->table.c.x
                    # matches a1.c.x->table.c.x better than
                    # selectable.c.x->table.c.x does.

                    col, intersect = c, i
                elif i == intersect:
                    # they have the same field of correspondence. see
                    # which proxy_set has fewer columns in it, which
                    # indicates a closer relationship with the root
                    # column. Also take into account the "weight"
                    # attribute which CompoundSelect() uses to give
                    # higher precedence to columns based on vertical
                    # position in the compound statement, and discard
                    # columns that have no reference to the target
                    # column (also occurs with CompoundSelect)

                    col_distance = util.reduce(
                        operator.add,
                        [
                            sc._annotations.get("weight", 1)
                            for sc in col._uncached_proxy_set()
                            if sc.shares_lineage(column)
                        ],
                    )
                    c_distance = util.reduce(
                        operator.add,
                        [
                            sc._annotations.get("weight", 1)
                            for sc in c._uncached_proxy_set()
                            if sc.shares_lineage(column)
                        ],
                    )
                    if c_distance < col_distance:
                        col, intersect = c, i
        return col


class DedupeColumnCollection(ColumnCollection):
    """A :class:`.ColumnCollection that maintains deduplicating behavior.

    This is useful by schema level objects such as :class:`.Table` and
    :class:`.PrimaryKeyConstraint`.    The collection includes more
    sophisticated mutator methods as well to suit schema objects which
    require mutable column collections.

    .. versionadded: 1.4

    """

    def add(self, column, key=None):
        if key is not None and column.key != key:
            raise exc.ArgumentError(
                "DedupeColumnCollection requires columns be under "
                "the same key as their .key"
            )
        key = column.key

        if key is None:
            raise exc.ArgumentError(
                "Can't add unnamed column to column collection"
            )

        if key in self._index:

            existing = self._index[key]

            if existing is column:
                return

            self.replace(column)

            # pop out memoized proxy_set as this
            # operation may very well be occurring
            # in a _make_proxy operation
            util.memoized_property.reset(column, "proxy_set")
        else:
            l = len(self._collection)
            self._collection.append((key, column))
            self._colset.add(column)
            self._index[l] = column
            self._index[key] = column

    def _populate_separate_keys(self, iter_):
        """populate from an iterator of (key, column)"""
        cols = list(iter_)

        replace_col = []
        for k, col in cols:
            if col.key != k:
                raise exc.ArgumentError(
                    "DedupeColumnCollection requires columns be under "
                    "the same key as their .key"
                )
            if col.name in self._index and col.key != col.name:
                replace_col.append(col)
            elif col.key in self._index:
                replace_col.append(col)
            else:
                self._index[k] = col
                self._collection.append((k, col))
        self._colset.update(c for (k, c) in self._collection)
        self._index.update(
            (idx, c) for idx, (k, c) in enumerate(self._collection)
        )
        for col in replace_col:
            self.replace(col)

    def extend(self, iter_):
        self._populate_separate_keys((col.key, col) for col in iter_)

    def remove(self, column):
        if column not in self._colset:
            raise ValueError(
                "Can't remove column %r; column is not in this collection"
                % column
            )
        del self._index[column.key]
        self._colset.remove(column)
        self._collection[:] = [
            (k, c) for (k, c) in self._collection if c is not column
        ]
        self._index.update(
            {idx: col for idx, (k, col) in enumerate(self._collection)}
        )
        # delete higher index
        del self._index[len(self._collection)]

    def replace(self, column):
        """add the given column to this collection, removing unaliased
           versions of this column  as well as existing columns with the
           same key.

            e.g.::

                t = Table('sometable', metadata, Column('col1', Integer))
                t.columns.replace(Column('col1', Integer, key='columnone'))

            will remove the original 'col1' from the collection, and add
            the new column under the name 'columnname'.

           Used by schema.Column to override columns during table reflection.

        """

        remove_col = set()
        # remove up to two columns based on matches of name as well as key
        if column.name in self._index and column.key != column.name:
            other = self._index[column.name]
            if other.name == other.key:
                remove_col.add(other)

        if column.key in self._index:
            remove_col.add(self._index[column.key])

        new_cols = []
        replaced = False
        for k, col in self._collection:
            if col in remove_col:
                if not replaced:
                    replaced = True
                    new_cols.append((column.key, column))
            else:
                new_cols.append((k, col))

        if remove_col:
            self._colset.difference_update(remove_col)

        if not replaced:
            new_cols.append((column.key, column))

        self._colset.add(column)
        self._collection[:] = new_cols

        self._index.clear()
        self._index.update(
            {idx: col for idx, (k, col) in enumerate(self._collection)}
        )
        self._index.update(self._collection)


class ImmutableColumnCollection(util.ImmutableContainer, ColumnCollection):
    __slots__ = ("_parent",)

    def __init__(self, collection):
        object.__setattr__(self, "_parent", collection)
        object.__setattr__(self, "_colset", collection._colset)
        object.__setattr__(self, "_index", collection._index)
        object.__setattr__(self, "_collection", collection._collection)

    def __getstate__(self):
        return {"_parent": self._parent}

    def __setstate__(self, state):
        parent = state["_parent"]
        self.__init__(parent)

    add = extend = remove = util.ImmutableContainer._immutable


class ColumnSet(util.ordered_column_set):
    def contains_column(self, col):
        return col in self

    def extend(self, cols):
        for col in cols:
            self.add(col)

    def __add__(self, other):
        return list(self) + list(other)

    def __eq__(self, other):
        l = []
        for c in other:
            for local in self:
                if c.shares_lineage(local):
                    l.append(c == local)
        return elements.and_(*l)

    def __hash__(self):
        return hash(tuple(x for x in self))


def _bind_or_error(schemaitem, msg=None):
    bind = schemaitem.bind
    if not bind:
        name = schemaitem.__class__.__name__
        label = getattr(
            schemaitem, "fullname", getattr(schemaitem, "name", None)
        )
        if label:
            item = "%s object %r" % (name, label)
        else:
            item = "%s object" % name
        if msg is None:
            msg = (
                "%s is not bound to an Engine or Connection.  "
                "Execution can not proceed without a database to execute "
                "against." % item
            )
        raise exc.UnboundExecutionError(msg)
    return bind
