#coding: utf-8
"""
The core module defines the Operator class. Operators are functions
which can be added, composed or multiplied by a scalar. See the
Operator docstring for more information.
"""

from __future__ import division

import copy
import inspect
import numpy as np
import operator
import scipy.sparse.linalg
import types

from collections import MutableMapping, MutableSequence, MutableSet, namedtuple
from . import memory
from .utils import (all_eq, first_is_not, isclassattr, isscalar, merge_none,
                    ndarraywrap, operation_assignment, strenum, strshape,
                    tointtuple)
from .utils.mpi import MPI
from .decorators import (linear, real, idempotent, involutary, square,
                         symmetric, universal, inplace)

__all__ = [
    'Operator',
    'OperatorFlags',
    'AdditionOperator',
    'BlockColumnOperator',
    'BlockDiagonalOperator',
    'BlockRowOperator',
    'BlockSliceOperator',
    'BroadcastingOperator',
    'CompositionOperator',
    'ConstantOperator',
    'DiagonalOperator',
    'HomothetyOperator',
    'IdentityOperator',
    'MultiplicationOperator',
    'ReshapeOperator',
    'ZeroOperator',
    'DirectOperatorFactory',
    'ReverseOperatorFactory',
    'asoperator',
    'I',
    'O',
]


class OperatorFlags(namedtuple('OperatorFlags',
                               [
                                'linear',
                                'square',     # shapein == shapeout
                                'real',       # o.C = o
                                'symmetric',  # o.T = o
                                'hermitian',  # o.H = o
                                'idempotent', # o * o = o
                                'involutary', # o * o = I
                                'orthogonal', # o * o.T = I
                                'unitary',    # o * o.H = I
                                'universal',  # o*[B1...Bn] = [o*B1...o*Bn]
                                'inplace',
                                'inplace_reduction',
                                'shape_input',
                                'shape_output',
                                ])):
    """ Informative flags about the operator. """
    def __new__(cls):
        t = 12*(False,) + ('', '')
        return super(OperatorFlags, cls).__new__(cls, *t)

    def __str__(self):
        n = max([len(f) for f in self._fields])
        fields = [ '  ' + f.upper().ljust(n) + ' : ' for f in self._fields]
        return '\n'.join([f + str(v) for f,v in zip(fields,self)])

    def __repr__(self):
        n = max([len(f) for f in self._fields])
        fields = [ f.ljust(n) + '= ' for f in self._fields]
        return self.__class__.__name__ + '(\n  ' + ',\n  '.join([f + repr(v) \
            for f,v in zip(fields,self)]) + ')'


class OperatorRule(object):
    """
    Abstract class for operator rules.

    A operator rule is a relation that can be expressed by the sentence
    "'subjects' are 'predicate'". An instance of this class, when called with
    checks if the inputs are subjects to the rule, and returns the predicate
    if it is the case. Otherwise, it returns None.
    """
    def __init__(self, subjects, predicate):

        if not isinstance(subjects, str):
            raise TypeError("The input first argument '{0}' is not a string." \
                             .format(subjects))

        subjects_ = self.split_subject(subjects)
        if len(subjects_) == 0:
            raise ValueError('No rule subject is specified.')
        if not isinstance(self, OperatorUnaryRule) and len(subjects_) == 1:
            self.__class__ = OperatorUnaryRule
            self.__init__(subjects, predicate)
            return
        if not isinstance(self, OperatorBinaryRule) and len(subjects_) == 2:
            self.__class__ = OperatorBinaryRule
            self.__init__(subjects, predicate)
            return

        if '1' in subjects_:
            raise ValueError("'1' cannot be a subject.")

        if isinstance(predicate, str) and '{' in predicate:
            raise ValueError("Predicate cannot be a subclass.")

        self.subjects = subjects_
        self.predicate = predicate

    def __eq__(self, other):
        if not isinstance(other, OperatorRule):
            return NotImplemented
        if self.subjects != other.subjects:
            return False
        if isinstance(self.predicate, types.LambdaType):
            if type(self.predicate) is not type(other.predicate):
                return False
            return self.predicate.func_code is other.predicate.func_code
        if isinstance(self.predicate, str):
            return self.predicate == other.predicate
        return self.predicate  is other.predicate

    @staticmethod
    def _symbol2operator(op, symbol):
        if not isinstance(symbol, str):
            return symbol
        if  symbol == '1':
            return IdentityOperator()
        if symbol[0] == '{' and symbol[-1] == '}':
            return symbol[1:-1]
        if symbol == '.':
            return op
        try:
            return {'.C': op.C,
                    '.T': op.T,
                    '.H': op.H,
                    '.I': op.I}[symbol]
        except (KeyError):
            raise ValueError("Invalid symbol: '{0}'.".format(symbol))

    @classmethod
    def split_subject(cls, subject):
        if isinstance(subject, (list, tuple)):
            return subject
        if not isinstance(subject, str):
            raise TypeError('The rule subject is not a string.')
        if len(subject) == 0:
            return []
        associated = '.IC', '.IT', '.IH', '.C', '.T', '.H', '.I', '.'
        for a in associated:
            if subject[:len(a)] == a:
                return [a] + cls.split_subject(subject[len(a):])
        if subject[0] == '{':
            try:
                pos = subject.index('}')
            except ValueError:
                raise ValueError("Invalid subject: no matching closing '}'.")
            return [subject[:pos+1]] + cls.split_subject(subject[pos+1:])

        raise ValueError("The subject {0} is not understood.".format(subject))

    def __str__(self):
        return '{0} = {1}'.format(''.join(self.subjects), self.predicate)

    __repr__ = __str__


class OperatorUnaryRule(OperatorRule):
    """
    Binary rule on operators.

    A operator unary rule is a relation that can be expressed by the sentence
    "'subject' is 'predicate'".

    Parameters
    ----------
    subject : str
        It defines the property of the operator for which the predicate holds:
            '.C' : the operator conjugate
            '.T' : the operator transpose
            '.H' : the operator adjoint
            '.I' : the operator adjoint
            '.IC' : the operator inverse-conjugate
            '.IT' : the operator inverse-transpose
            '.IH' : the operator inverse-adjoint

    predicate : function or str
        What is returned by the rule when is applies. It can be:
            '1' : the identity operator
            '.' : the operator itself
            or a callable of one argument.
    
    Example
    -------
    >>> rule = OperatorUnaryRule('.T', '.')
    >>> o = Operator()
    >>> oT = rule(o)
    >>> oT is o
    True
    
    """
    def __init__(self, subjects, predicate):
        super(OperatorUnaryRule, self).__init__(subjects, predicate)
        if len(self.subjects) != 1:
            raise ValueError('This is not a unary rule.')
        if self.subjects[0] == '.':
            raise ValueError('The subject cannot be the operator itself.')
        if callable(predicate) or predicate in ('.', '1'):
            return
        raise ValueError("Invalid predicate: '{0}'.".format(predicate))

    def __call__(self, reference):
        predicate = self._symbol2operator(reference, self.predicate)
        if not isinstance(predicate, Operator ) and callable(predicate):
            predicate = predicate(reference)
        if not isinstance(predicate, Operator):
            raise TypeError('The predicate is not an operator.')
        return predicate


class OperatorBinaryRule(OperatorRule):
    """
    Binary rule on operators.

    A operator rule is a relation that can be expressed by the sentence
    "'subjects' are 'predicate'". An instance of this class, when called with
    two input arguments checks if the inputs are subjects to the rule, and
    returns the predicate if it is the case. Otherwise, it returns None.

    Parameters
    ----------
    subjects : str
        It defines the relationship between the two subjects that must be
        verified for the rule to apply. It is the concatenation of two
        expressions. One has to be '.' and stands for the reference subject.
        It determines if the reference operator is on the right or left hand
        side of the operator pair. The other expression constrains the other
        subject, which must be:
            '.' : the reference operator itself.
            '.C' : the conjugate of the reference object
            '.T' : the transpose of the reference object
            '.H' : the adjoint of the reference object
            '{...}' : an instance of the class '...'
            '{self}': an instance of the reference operator's class
        For instance, given a string '.C.', the rule will apply to the inputs
        o1 and o2 if o1 is o2.C. For a condition '.{DiagonalOperator}', the
        rule will apply if o2 is a DiagonalOperator instance.

    predicate : function or str
        If the two objects o1, o2, are subjects of the rule, the predicate
        will be returned. The predicate can also be '1', '.', '.C', '.T', '.H'
        of a callable of two arguments.
    
    Example
    -------
    >>> rule = OperatorBinaryRule('..', '.')
    >>> o = Operator()
    >>> rule(o, o) is o
    True
    >>> rule(o, IdentityOperator()) is None
    True
    
    """
    def __init__(self, subjects, predicate):
        super(OperatorBinaryRule, self).__init__(subjects, predicate)
        if len(self.subjects) != 2:
            raise ValueError('This is not a binary rule.')
        self.reference = 1 if self.subjects[1] == '.' else 0
        self.other = self.subjects[1-self.reference]

    def __call__(self, o1, o2):

        reference, other = (o1,o2) if self.reference == 0 else (o2,o1)
        subother = self._symbol2operator(reference, self.other)
        predicate = self._symbol2operator(reference, self.predicate)

        if isinstance(subother, str):
            if subother == 'self':
                if not isinstance(other, reference.__class__):
                    return None
            elif subother not in (c.__name__ for c in other.__class__.__mro__):
                return None
        elif other is not subother:
            return None

        if not isinstance(predicate, Operator) and callable(predicate):
            predicate = predicate(o1, o2)
        if predicate is None:
            return None
        if isinstance(predicate, (list,tuple)) and len(predicate) == 1:
            predicate = predicate[0]
        if not isinstance(predicate, Operator) and not (isinstance(predicate,
           (list,tuple)) and all([isinstance(o, Operator) for o in predicate])):
            raise TypeError("The predicate '{0}' is not an operator.".format(
                            predicate))
        return predicate


class Operator(object):
    """
    Operator top-level class.

    The operator class is a function factory.

    Attributes
    ----------
    attrin/attrout : dict or function
        If attrout is a dict, its items are added to the output. If it is
        a function, it takes the input attributes and returns the output
        attributes. The attrin attribute is only used in the reversed direction.
    classin/classout : ndarray subclass
        The classout attribute sets the output class. The classin attribute is
        only used in the reversed direction.
    commin/commout : mpi4py.Comm
        The commin and commout attributes store the MPI communicator for the in-
        put and output.
    reshapein/reshapeout : function
        The reshapein function takes the input shape and returns the output
        shape. The method is used for implicit output shape operators.
        The reshapeout function does the opposite.
    shapein : tuple
        Operator's input shape.
    shapeout : tuple
        Operator's output shape.
    toshapein/toshapeout : function
        The toshapein function reshapes a vector into a multi-dimensional array
        compatible with the operator's input shape. The toshapeout method is
        only used in the reversed direction.
    validatein/validateout : function
        The validatein function raises a ValueError exception if the input
        shape is not valid. The validateout function is used in the reversed
        direction
    flags : OperatorFlags
        The flags describe properties of the operator.
    dtype : dtype
        The operator's dtype is used to determine the dtype of its output.
        Unless it is None, the output dtype is the common type of the operator
        and input dtypes. If dtype is None, the output dtype is the input
        dtype.
    C : Operator
        Oonjugate operator.
    T : Operator
        Tranpose operator.
    H : Operator
        Adjoint operator.
    I : Operator
        Inverse operator.

    """
    def __init__(self, direct=None, transpose=None, adjoint=None,
                 conjugate_=None, inverse=None, inverse_transpose=None,
                 inverse_adjoint=None, inverse_conjugate=None,
                 attrin={}, attrout={}, classin=None, classout=None,
                 commin=None, commout=None, reshapein=None, reshapeout=None,
                 shapein=None, shapeout=None, toshapein=None, toshapeout=None,
                 validatein=None, validateout=None, dtype=None, flags={}):
            
        for method, name in zip( \
            (direct, transpose, adjoint, conjugate_, inverse, inverse_transpose,
             inverse_adjoint, inverse_conjugate),
            ('direct', 'transpose', 'adjoint', 'conjugate_', 'inverse',
             'inverse_transpose', 'inverse_adjoint', 'inverse_conjugate')):
            if method is not None:
                if not hasattr(method, '__call__'):
                    raise TypeError("The method '%s' is not callable." % name)
                # should also check that the method has at least two arguments
                setattr(self, name, method)

        if self.transpose is None and self.adjoint is not None:
            def transpose(input, output):
                self.adjoint(input.conjugate(), output)
                output[...] = output.conjugate()
            self.transpose = transpose

        if self.adjoint is None and self.transpose is not None:
            def adjoint(input, output):
                self.transpose(input.conjugate(), output)
                output[...] = output.conjugate()

        if self.inverse is None:
            self.inverse_conjugate = None

        self._C = self._T = self._H = self._I = None

        self._init_dtype(dtype)
        self._init_flags(flags)
        self._init_rules()
        self._init_name()
        self._init_inout(attrin, attrout, classin, classout, commin, commout,
                         reshapein, reshapeout, shapein, shapeout, toshapein,
                         toshapeout, validatein, validateout)

    dtype = None
    flags = OperatorFlags()

    attrin = {}
    attrout = {}
    classin = None
    classout = None
    commin = None
    commout = None
    reshapein = None
    reshapeout = None
    shapein = None
    shapeout = None

    def toshapein(self, v):
        """
        Reshape a vector into a multi-dimensional array compatible with
        the operator's input shape.
        """
        if self.shapein is None:
            raise ValueError("The operator '" + self.__name__ + "' does not hav"
                             "e an explicit shape.")
        return v.reshape(self.shapein)

    def toshapeout(self, v):
        """
        Reshape a vector into a multi-dimensional array compatible with
        the operator's output shape.
        """
        if self.shapeout is None:
            raise ValueError("The operator '" + self.__name__ + "' does not hav"
                             "e an explicit shape.")
        return v.reshape(self.shapeout)

    def propagate_commin(self, commin):
        """
        Propagate MPI communicator of the input to the operands.
        Operands have the possibility to change during this step.

        """
        return self

    def propagate_commout(self, commin):
        """
        Propagate MPI communicator of the output to the operands.
        Operands have the possibility to change during this step.

        """
        return self

    def validatein(self, shapein):
        """
        Validate an input shape by raising a ValueError exception if it is
        invalid.
        """
        if self.shapein is not None and self.shapein != shapein:
            raise ValueError("The input shape '{0}' is incompatible with that o"
                "f {1}: '{2}'.".format(shapein, self.__name__, self.shapein))

    def validateout(self, shapeout):
        """
        Validate an output shape by raising a ValueError exception if it is
        invalid.
        """
        if self.shapeout is not None and self.shapeout != shapeout:
            raise ValueError("The output shape '{0}' is incompatible with that "
                "of {1}: '{2}'.".format(shapeout, self.__name__, self.shapeout))
    
    direct = None
    def conjugate_(self, input, output):
        self.direct(input.conjugate(), output)
        output[...] = output.conjugate()
    transpose = None
    adjoint = None
    inverse = None
    def inverse_conjugate(self, input, output):
        self.inverse(input.conjugate(), output)
        output[...] = output.conjugate()
    inverse_transpose = None
    inverse_adjoint = None

    def __call__(self, x, out=None):

        if isinstance(x, Operator):
            return CompositionOperator([self, x])

        if self.direct is None:
            raise NotImplementedError('Call to ' + self.__name__ + ' is not imp'
                                      'lemented.')
        i, o = self._validate_arguments(x, out)
        with memory.push_and_pop(o):
            if not self.flags.inplace and self.same_data(i, o):
                memory.up()
                o_ = memory.get(o.nbytes, o.shape, o.dtype, self.__name__) \
                           .view(o.dtype).reshape(o.shape)
            else:
                o_ = o
            self.direct(i, o_)
            if not self.flags.inplace and self.same_data(i, o):
                memory.down()
                o[...] = o_

        cls = x.__class__ if isinstance(x, np.ndarray) else np.ndarray
        attr = x.__dict__.copy() if hasattr(x, '__dict__') else {}
        cls = self.propagate_attributes(cls, attr)
        if cls is np.ndarray and len(attr) > 0:
            cls = ndarraywrap
        if out is None:
            out = o
        if type(out) is np.ndarray:
            if cls is np.ndarray:
                return out
            out = out.view(cls)
        elif type(out) is not cls:
            out.__class__ = cls
            if out.__array_finalize__ is not None:
                out.__array_finalize__()

        # we cannot simply update __dict__, because of properties.
        # the iteration is sorted by key, so that attributes beginning with an
        # underscore are set first.
        for k in sorted(attr.keys()):
            setattr(out, k, attr[k])
        return out

    @property
    def shape(self):
        shape = (np.product(self.shapeout), np.product(self.shapein))
        if shape[0] is None or shape[1] is None:
            return None
        return shape

    @staticmethod
    def same_data(array1, array2):
        return array1.__array_interface__['data'][0] == \
               array2.__array_interface__['data'][0]

    def todense(self, shapein=None, inplace=False):
        """
        Output the dense representation of the Operator
        as a ndarray.

        Arguments
        ---------
        shapein : (default None) None or tuple
            If a shapein is not already associated with the Operator,
            it must me passed to the todense method.
        inplace : boolean
            For testing purposes only. By default, this method uses
            out-of-place operations that directly fill the output array.
            By setting inplace to True, one can test in-place operations, at
            the cost of additional copies.
        """
        if not self.flags.linear:
            raise TypeError('The operator is not linear.')
        shapein = tointtuple(shapein) if shapein is not None else self.shapein
        if shapein is None:
            raise ValueError("The operator has not an explicit input shape. Use"
                             " the 'shapein' keyword.")
        shapeout = self.validatereshapein(shapein)
        m, n = np.product(shapeout), np.product(shapein)
        d = np.empty((n,m), self.dtype)

        if not inplace or not self.flags.inplace:
            v = np.zeros(n, self.dtype)
            for i in xrange(n):
                v[i] = 1
                o = d[i,:].reshape(shapeout)
                with memory.push_and_pop(o):
                    self.direct(v.reshape(shapein), o)
                v[i] = 0
            return d.T

        # test in-place mechanism
        u = np.empty(max(m,n), self.dtype)
        v = u[:n]
        w = u[:m]
        for i in xrange(n):
            v[:] = 0
            v[i] = 1
            with memory.push_and_pop(w):
                self.direct(v.reshape(shapein), w.reshape(shapeout))
            d[i,:] = w
        return d.T

    def matvec(self, v, output=None):
        v = self.toshapein(v)
        if output is not None:
            output = self.toshapeout(output)
        input, output = self._validate_arguments(v, output)
        with memory.push_and_pop(output):
            self.direct(input, output)
        return output.view(np.ndarray).ravel()

    def rmatvec(self, v, output=None):
        return self.T.matvec(v, output)

    def set_rule(self, subjects, predicate, operation=None, globals=None,
                 merge=True):
        """
        Add a rule to the rule list, taking care of duplicates and priorities.
        Class-matching rules have a lower priority than the others.

        Parameters
        ----------
        subjects : str
            See OperatorUnaryRule and OperatorBinaryRule documentation.
        predicate : str
            See OperatorUnaryRule and OperatorBinaryRule documentation.
        operation : CompositeOperator sub class
            Operation to which applies the rule. It can be: CompositionOperator,
            AdditionOperator and MultiplicationOperator. For unary rules,
            the value must be None.
        globals : dict, optional
            Dictionary containing the operator classes used in class-matching
            rules. It is required for classes not from pyoperators.ocre and for
            which more than one class-matching rule is set.
        """
        rule = OperatorRule(subjects, predicate)
        rule.merge = merge

        if len(rule.subjects) > 2:
            raise ValueError('Only unary and binary rules are allowed.')

        if operation is None and len(rule.subjects) == 2:
            raise ValueError('The operation is not specified.')

        # get the rule list for the specified operation
        if operation is None:
            if None not in self.rules:
                self.rules[None] = []
            rules = self.rules[None]
        elif issubclass(operation, CommutativeCompositeOperator):
            if rule.subjects[-1] == '.':
                rule.subjects = rule.subjects[::-1]
                rule.reference = 0
            if operation not in self.rules:
                self.rules[operation] = []
            rules = self.rules[operation]
        else:
            if operation not in self.rules:
                self.rules[operation] = {'left':[], 'right':[]}
            rules = self.rules[operation]['left' if rule.reference == 0 else \
                                          'right']
        ids = [r.subjects for r in rules]

        # first, try to override existing rule
        try:
            index = ids.index(rule.subjects)
            rules[index] = rule
            return
        except ValueError:
            pass

        if len(rule.subjects) == 1 or not rule.other.startswith('{'):
            rules.insert(0, rule)
            return

        # search for subclass rules
        try:
            index = [r.other[0] for r in rules].index('{')
        except ValueError:
            rules.append(rule)
            return

        # insert the rule after more specific ones
        cls = type(self) if rule.other[1:-1] == 'self' else \
              eval(rule.other[1:-1], globals)
        classes = [ r.other[1:-1] for r in rules[index:] ]
        classes = [ cls if r == 'self' else eval(r, globals) for r in classes ]
        is_subclass = [ issubclass(cls, c) for c in classes ]
        is_supclass = [ issubclass(c, cls) for c in classes ]
        try:
            index2 = is_subclass.index(True)
        except ValueError:
            try:
                index2 = len(is_supclass) - is_supclass[::-1].index(True)
            except ValueError:
                index2 = 0
        rules.insert(index + index2, rule)

    def del_rule(self, subjects, operation=None):
        """
        Delete an operator rule.

        If the rule does not exist, a ValueError exception is raised.

        Parameters
        ----------
        subjects : str
            The subjects of the rule to be deleted.
        operation : CompositeOperator sub class
            Operation to which applies the rule to be deleted. It can be:
            CompositionOperator, AdditionOperator and MultiplicationOperator.
            For unary rules, the value must be None.
        """
        subjects = OperatorRule.split_subject(subjects)
        if len(subjects) > 2:
            raise ValueError('Only unary and binary rules are allowed.')
        if operation is None and len(subjects) == 2:
            raise ValueError('The operation is not specified.')
        if operation not in self.rules:
            if None not in self.rules:
                raise ValueError('There is no unary rule.')
            raise ValueError("The operation '{0}' has no rules.".format(type(
                             operation).__name__))
        rules = self.rules[operation]
        if operation is not None:
            right = subjects[-1] == '.'
            if issubclass(operation, CommutativeCompositeOperator):
                if right:
                    subjects = subjects[::-1]
            else:
                rules = rules['right' if right else 'left']
        index = [r.subjects for r in rules].index(subjects)
        del rules[index]
        
    @property
    def C(self):
        """ Return the complex-conjugate of the operator. """
        if self._C is None:
            self._generate_associated_operators()
        return self._C

    @property
    def T(self):
        """ Return the transpose of the operator. """
        if self._T is None:
            self._generate_associated_operators()
        return self._T

    @property
    def H(self):
        """ Return the adjoint of the operator. """
        if self._H is None:
            self._generate_associated_operators()
        return self._H

    @property
    def I(self):
        """ Return the inverse of the operator. """
        if self._I is None:
            self._generate_associated_operators()
        return self._I

    def conjugate(self):
        """ Return the complex-conjugate of the operator. Same as '.C'. """
        return self.C

    def copy(self):
        """ Return a copy of the operator. """
        return copy.copy(self)

    def propagate_attributes(self, cls, attr):
        """
        Propagate attributes according to operator's output attributes.
        If the class changes, class attributes are removed if they are
        not class attributes of the new class.
        """
        if None not in (self.classout, cls) and self.classout is not cls:
            for a in attr.keys():
                if isclassattr(cls, a) and not isclassattr(self.classout, a):
                    del attr[a]
        if 'shape_global' in attr:
            del attr['shape_global']
        if isinstance(self.attrout, dict):
            for k, v in self.attrout.items():
                if isinstance(v, (MutableMapping, MutableSequence, MutableSet)):
                    if hasattr(v, 'copy'):
                        v = v.copy()
                    elif type(v) is list:
                        v = list(v)
                attr[k] = v
        else:
            self.attrout(attr)
        return self.classout or cls
            
    def validatereshapein(self, shapein):
        """
        Return the operator's output shape for a given input shape.
        
        If the operator has not an explicit output shape, the specified input
        shape is used to deduce it.

        Parameter
        ---------
        shapein : tuple
            The input shape.

        Returns
        -------
        shapeout : tuple
            The output shape, consistent with the input shape
        """
        shapein = tointtuple(shapein)
        if shapein is not None:
            self.validatein(shapein)
        if self.shapeout is not None:
            # explicit output shape
            return self.shapeout
        if self.reshapein is None or shapein is None:
            # unconstrained output shape (or shapein is None)
            return None
        # implicit output shape
        shapeout = tointtuple(self.reshapein(shapein))
        self.validateout(shapeout)
        return shapeout

    def validatereshapeout(self, shapeout):
        """
        Return the operator's input shape for a given output shape.
        
        If the operator has not an explicit input shape, the specified output
        shape is used to deduce it.

        Parameter
        ---------
        shapeout : tuple
            The output shape.

        Returns
        -------
        shapein : tuple
            The input shape, consistent with the output shape
        """
        shapeout = tointtuple(shapeout)
        if shapeout is not None:
            self.validateout(shapeout)
        if self.shapein is not None:
            # explicit input shape
            return self.shapein
        if self.reshapeout is None or shapeout is None:
            # unconstrained input shape (or shapeout is None)
            return None
        # implicit input shape
        shapein = tointtuple(self.reshapeout(shapeout))
        self.validatein(shapein)
        return shapein

    @staticmethod
    def _find_common_type(dtypes):
        """ Return dtype of greater type rank. """
        dtypes = [d for d in dtypes if d is not None]
        if len(dtypes) == 0:
            return None
        return np.find_common_type(dtypes, [])

    def _generate_associated_operators(self):
        """
        Compute at once the conjugate, transpose, adjoint and inverse operators
        of the instance and of themselves.
        """
        rules = dict((r.subjects[0],r) for r in self.rules[None])

        if self.flags.real:
            C = self
        elif '.C' in rules:
            C = rules['.C'](self)
        else:
            C = DirectOperatorFactory(Operator, self, direct=self.conjugate_)
            C.__name__ = self.__name__ + '.C'

        if self.flags.symmetric:
            T = self
        elif '.T' in rules:
            T = rules['.T'](self)
        else:
            T = ReverseOperatorFactory(Operator, self, direct=self.transpose)
            T.__name__ = self.__name__ + '.T'

        if self.flags.hermitian:
            H = self
        elif '.H' in rules:
            H = rules['.H'](self)
        elif self.flags.real:
            H = T
        elif self.flags.symmetric:
            H = C
        else:
            H = ReverseOperatorFactory(Operator, self, direct=self.adjoint)
            H.__name__ = self.__name__ + '.H'

        if self.flags.involutary:
            I = self
        elif '.I' in rules:
            I = rules['.I'](self)
        elif self.flags.orthogonal:
            I = T
        elif self.flags.unitary:
            I = H
        else:
            I = ReverseOperatorFactory(Operator, self, direct=self.inverse)
            I.__name__ = self.__name__ + '.I'

        if self.flags.real:
            IC = I
        elif '.IC' in rules:
            IC = rules['.IC'](self)
        elif self.flags.orthogonal:
            IC = H
        elif self.flags.unitary:
            IC = T
        elif self.flags.involutary:
            IC = C
        else:
            IC = ReverseOperatorFactory(Operator, self,
                                        direct=self.inverse_conjugate)
            IC.__name__ = self.__name__ + '.I.C'

        if self.flags.orthogonal:
            IT = self
        elif self.flags.symmetric:
            IT = I
        elif self.flags.unitary:
            IT = C
        elif self.flags.involutary:
            IT = T
        elif '.IT' in rules:
            IT = rules['.IT'](self)
        else:
            IT = DirectOperatorFactory(Operator, self,
                                       direct=self.inverse_transpose)
            IT.__name__ = self.__name__ + '.I.T'

        if self.flags.unitary:
            IH = self
        elif self.flags.hermitian:
            IH = I
        elif self.flags.orthogonal:
            IH = C
        elif self.flags.involutary:
            IH = H
        elif self.flags.symmetric:
            IH = IC
        elif self.flags.real:
            IH = IT
        elif '.IH' in rules:
            IH = rules['.IH'](self)
        else:
            IH = DirectOperatorFactory(Operator, self,
                                       direct=self.inverse_adjoint)
            IH.__name__ = self.__name__ + '.I.H'

        # once all the associated operators are instanciated, we set all their
        # associated operators. To do so, we use the fact that the transpose,
        # adjoint, conjugate and inverse operators are commutative and 
        # involutary.
        self._C, self._T, self._H, self._I = C, T, H, I
        C._C, C._T, C._H, C._I = self, H, T, IC
        T._C, T._T, T._H, T._I = H, self, C, IT
        H._C, H._T, H._H, H._I = T, C, self, IH
        I._C, I._T, I._H, I._I = IC, IT, IH, self
        IC._C, IC._T, IC._H, IC._I = I, IH, IT, C
        IT._C, IT._T, IT._H, IT._I = IH, I, IC, T
        IH._C, IH._T, IH._H, IH._I = IT, IC, I, H

    def _init_dtype(self, dtype):
        """ A non-complex dtype sets the real flag to true. """
        if dtype is not None:
            dtype = np.dtype(dtype)
        self.dtype = dtype
        if self.dtype is None or self.dtype.kind != 'c':
            self._set_flags('real')

    def _init_flags(self, flags):

# XXX reset me...
#        if 'flags' in self.__dict__:
#            del self.__dict__['flags']

        self._set_flags(flags)

        if self.flags.real:
            if self.flags.symmetric:
                self._set_flags('hermitian')
            if self.flags.hermitian:
                self._set_flags('symmetric')
            if self.flags.orthogonal:
                self._set_flags('unitary')
            if self.flags.unitary:
                self._set_flags('orthogonal')

        if self.flags.orthogonal:
            if self.flags.idempotent:
                self._set_flags('symmetric')
            if self.flags.symmetric:
                self._set_flags('idempotent')

        if self.flags.unitary:
            if self.flags.idempotent:
                self._set_flags('hermitian')
            if self.flags.hermitian:
                self._set_flags('idempotent')

        if self.flags.involutary:
            if self.flags.symmetric:
                self._set_flags('orthogonal')
            if self.flags.orthogonal:
                self._set_flags('symmetric')
            if self.flags.hermitian:
                self._set_flags('unitary')
            if self.flags.unitary:
                self._set_flags('hermitian')

        if self.flags.idempotent:
            if any([self.flags.orthogonal, self.flags.unitary,
                    self.flags.involutary]):
                self._set_flags('orthogonal, unitary, involutary')

        if isinstance(self.direct, np.ufunc):
            self._set_flags('inplace')
            self._set_flags('universal')
            if self.flags.inplace_reduction:
                raise ValueError('Ufuncs do not handle inplace reductions.')
        else:
            if isinstance(self.direct, types.MethodType):
                d = self.direct.im_func
            else:
                d = self.direct
            if isinstance(flags, (dict, str)) and 'inplace_reduction' not in \
               flags or isinstance(flags, OperatorFlags):
                if d is not None and 'operation' in d.func_code.co_varnames:
                    self._set_flags('inplace_reduction')
            if self.flags.inplace_reduction:
                if d is not None and 'operation' not in d.func_code.co_varnames:
                    raise TypeError("The direct method of an inplace-reduction "
                            "operator must have an 'operation' keyword.")


    def _init_rules(self):
        """ Translate flags into rules. """
        self.rules = {}

        if self.flags.real:
            self.set_rule('.C', '.')
        if self.flags.symmetric:
            self.set_rule('.T', '.')
        if self.flags.hermitian:
            self.set_rule('.H', '.')
        if self.flags.involutary:
            self.set_rule('.I', '.')

        self.set_rule('.I.', '1', CompositionOperator)
        if self.flags.orthogonal:
            self.set_rule('.T.', '1', CompositionOperator)
        if self.flags.unitary:
            self.set_rule('.H.', '1', CompositionOperator)
        if self.flags.idempotent:
            self.set_rule('..', '.', CompositionOperator)

    def _init_inout(self, attrin, attrout, classin, classout, commin, commout,
                    reshapein, reshapeout, shapein, shapeout, toshapein,
                    toshapeout, validatein, validateout):
        """
        Set methods and attributes dealing with the input and output handling.
        """

        # reset attributes
        for attr in ('attr', 'class', 'comm', 'reshape', 'shape', 'toshape',
                     'validate'):
            for inout in ('in', 'out'):
                if attr + inout in self.__dict__:
                    del self.__dict__[attr + inout]

        if isinstance(attrin, (dict, types.FunctionType, types.MethodType)):
            if not isinstance(attrin, dict) or len(attrin) > 0:
                self.attrin = attrin
        else:
            raise TypeError("The 'attrin' keyword should be a dictionary or a f"
                            "unction.")
        if isinstance(attrout, (dict, types.FunctionType, types.MethodType)):
            if not isinstance(attrout, dict) or len(attrout) > 0:
                self.attrout = attrout
        else:
            raise TypeError("The 'attrout' keyword should be a dictionary or a "
                            "function.")
        if type(classin) is type and issubclass(classin, np.ndarray):
            self.classin = classin
        elif classin is not None:
            raise TypeError("The 'classin' keyword is not an ndarray subclass.")
        if type(classout) is type and issubclass(classout, np.ndarray):
            self.classout = classout
        elif classout is not None:
            raise TypeError("The 'classout' keyword is not an ndarray subclass.")
        if commin is not None:
            self.commin = commin
        if commout is not None:
            self.commout = commout
        if reshapein is not None:
            self.reshapein = reshapein
        if reshapeout is not None:
            self.reshapeout = reshapeout
        if toshapein is not None:
            self.toshapein = toshapein
        if toshapeout is not None:
            self.toshapeout = toshapeout
        if validatein is not None:
            self.validatein = validatein
        if validateout is not None:
            self.validateout = validateout

        shapein = tointtuple(shapein)
        shapeout = tointtuple(shapeout)
        shapeout_, shapein_ = self.validatereshapein(shapein), \
                              self.validatereshapeout(shapeout)

        if None not in (shapein, shapein_) and shapein != shapein_:
            raise ValueError("The specified input shape '{0}' is incompatible w"
                "ith the expected one '{1}'.".format(shapein, shapein_))
        if None not in (shapeout, shapeout_) and shapeout != shapeout_:
            raise ValueError("The specified output shape '{0}' is incompatible "
                "with the expected one '{1}'.".format(shapeout, shapeout_))

        self.shapein = shapein if shapein is not None else shapein_
        self.shapeout = shapeout if shapeout is not None else shapeout_
        
        if self.shapein is not None and self.shapein == self.shapeout:
            self._set_flags('square')

        if self.flags.square:
            if self.shapein is None:
                self.shapein = self.shapeout
            else:
                self.shapeout = self.shapein
            self.reshapein = (lambda x:x) if self.shapeout is None else None
            self.reshapeout = self.reshapein
            self.validatein = self.validatein or self.validateout
            self.validateout = self.validatein
            if self.toshapein.im_func is Operator.toshapein.im_func and \
               self.toshapeout.im_func is not Operator.toshapeout.im_func:
                self.toshapein = self.toshapeout
            else:
                self.toshapeout = self.toshapein

        flag_is = 'explicit' if self.shapein is not None else 'implicit' \
                  if self.reshapeout is not None else 'unconstrained'
        flag_os = 'explicit' if self.shapeout is not None else 'implicit' \
                  if self.reshapein is not None else 'unconstrained'
        self._set_flags(shape_input=flag_is, shape_output=flag_os)

        if flag_is == 'explicit':
            self.validatein = Operator.validatein.__get__(self, type(self))
        if flag_os == 'explicit':
            self.validateout = Operator.validateout.__get__(self, type(self))
                    
    def _init_name(self):
        """ Set operator's __name__ attribute. """
        if self.__class__ != 'Operator':
            name = self.__class__.__name__
        elif self.direct and self.direct.__name__ not in ('<lambda>', 'direct'):
            name = self.direct.__name__
        else:
            name = 'Operator'
        self.__name__ = name

    def _set_flags(self, flags=None, **keywords):
        """ Set flags to an Operator. """
        if flags is None:
            flags = keywords
        if isinstance(flags, OperatorFlags):
            self.flags = flags
        elif isinstance(flags, (dict, list, tuple, str)):
            if isinstance(flags, str):
                flags = [f.strip() for f in flags.split(',')]
            elif isscalar(flags):
                flags = (flags,)
            if isinstance(flags, (list, tuple)):
                flags = dict((f,True) for f in flags)
            if any(not isinstance(f, str) for f in flags.keys()):
                raise TypeError("Invalid type for the operator flags: {0}." \
                                .format(flags))
            if any(f not in OperatorFlags._fields for f in flags):
                raise ValueError("Invalid operator flags '{0}'. The properties "
                    "must be one of the following: ".format(flags.keys()) + \
                    strenum(OperatorFlags._fields) + '.')
            self.flags = self.flags._replace(**flags)
            flags = [ f for f in flags if flags[f]]
            if 'symmetric' in flags or 'hermitian' in flags or \
               'orthogonal' in flags or 'unitary' in flags:
                self.flags = self.flags._replace(linear=True, square=True)
            if 'orthogonal' in flags:
                self.flags = self.flags._replace(real=True)
            if 'involutary' in flags:
                self.flags = self.flags._replace(square=True)
        elif flags is not None:
            raise TypeError("Invalid input flags: '{0}'.".format(flags))

    def _validate_arguments(self, input, output):
        """
        Return the input and output as ndarray instances.
        If required, allocate the output.
        """
        input = np.array(input, copy=False, subok=True)

        shapeout = self.validatereshapein(input.shape)
        dtype = self._find_common_type([input.dtype, self.dtype])
        input = np.array(input, dtype=dtype, subok=False, copy=False)
        if output is not None:
            if not isinstance(output, np.ndarray):
                raise TypeError('The output argument is not an ndarray.')
            if output.dtype != dtype:
                raise ValueError("The output has an invalid dtype '{0}'. Expect"
                    "ed dtype is '{1}'.".format(output.dtype, dtype))
            shapein = self.validatereshapeout(output.shape)
            if shapein is not None and shapein != input.shape:
                raise ValueError("The input has an invalid shape '{0}'. Expecte"
                    "d shape is '{1}'.".format(input.shape, shapein))
            if shapeout is not None and shapeout != output.shape:
                raise ValueError("The output has an invalid shape '{0}'. Expect"
                    "ed shape is '{1}'.".format(output.shape, shapeout))
            output = output.view(np.ndarray)
        else:
            if self.flags.shape_input == 'implicit' and \
               self.flags.shape_output == 'unconstrained':
                raise ValueError('The output shape of an implicit input shape a'
                    'nd unconstrained output shape operator cannot be inferred.'
                    )
            if shapeout is None:
                shapeout = input.shape
            output = memory.allocate(shapeout, dtype, 'in ' + self.__name__)
        return input, output

    def __mul__(self, other):
        if isinstance(other, np.ndarray):
            return self.matvec(other)
        return CompositionOperator([self, other])

    def __rmul__(self, other):
        if not isscalar(other):
            raise NotImplementedError("It is not possible to multiply '" + \
                str(type(other)) + "' with an Operator.")
        return CompositionOperator([other, self])

    def __imul__(self, other):
        return CompositionOperator([self, other])

    def __add__(self, other):
        return AdditionOperator([self, other])

    def __radd__(self, other):
        return AdditionOperator([other, self])

    def __iadd__(self, other):
        return AdditionOperator([self, other])

    def __sub__(self, other):
        return AdditionOperator([self, -other])

    def __rsub__(self, other):
        return AdditionOperator([other, -self])

    def __isub__(self, other):
        return AdditionOperator([self, -other])

    def __neg__(self):
        return HomothetyOperator(-1) * self

    def __eq__(self, other):
        if self is other:
            return True
        if type(self) is not type(other):
            return False
        d1 = self.__dict__.copy()
        d2 = other.__dict__.copy()
        for k in '_C', '_T', '_H', '_I', '_D':
            if k in d1: del d1[k]
            if k in d2: del d2[k]
        return all_eq(d1, d2)

    def __str__(self):
        if self.shapein is not None or self.shapeout is not None:
            shapein = '?' if self.shapein is None else strshape(self.shapein)
            shapeout = '?' if self.shapeout is None else strshape(self.shapeout)
            if self.flags.square and self.shapein is not None and \
               len(self.shapein) > 1:
                s = shapein + '²'
            else:
                s = shapeout + 'x' + shapein
            s += ' '
        else:
            s = ''
        if hasattr(self, '__name__'):
            s += self.__name__
        else:
            s += type(self).__name__ + '[not initialized]'
        return s

    def __repr__(self):
        a = []
        init = getattr(self, '__init_original__', self.__init__)
        vars, args, keywords, defaults = inspect.getargspec(init)
            
        for ivar, var in enumerate(vars):
            if var in ('flags', 'self'):
                continue
            if var == 'shapeout' and self.flags.shape_output == 'implicit':
                continue
            if var == 'shapein' and self.flags.shape_input == 'implicit':
                continue
            if var == 'reshapeout' and self.flags.square and \
               self.flags.shape_input == 'implicit':
                continue

            val = getattr(self, var, None)
            if isinstance(val, types.MethodType):
                continue
            nargs = len(vars) - (len(defaults) if defaults is not None else 0)
            if ivar >= nargs:
                try:
                    if val == defaults[ivar - nargs]:
                        continue
                except:
                    if val is defaults[ivar - nargs]:
                        continue
            if var == 'reshapein' and self.flags.square and \
               self.flags.shape_output == 'implicit':
                s = 'lambda x:x'
            elif var in ('commin', 'commout'):
                if val is MPI.COMM_WORLD:
                    s = 'MPI.COMM_WORLD'
                elif val is MPI.COMM_SELF:
                    s = 'MPI.COMM_SELF'
                else:
                    s = str(val)
            elif isinstance(val, Operator):
                s = 'Operator()'
            elif type(val) is type:
                s = val.__module__ + '.' + val.__name__
            elif var in ['shapein', 'shapeout']:
                s = strshape(val)
            elif isinstance(val, np.ndarray) and val.ndim == 0:
                s = repr(val[()])
            elif isinstance(val, np.ndarray):
                s = 'array' if type(val) is np.ndarray else type(val).__name__
                s += '(' + val.ndim * '['
                s += str(val.flat[0])
                if val.size > 1:
                    s += ', ' if val.size == 2 else ', ..., '
                    s += str(val.flat[-1])
                s += val.ndim * ']' +  ', dtype={0})'.format(val.dtype)
            elif var == 'dtype':
                s = str(val)
            else:
                s = repr(val)

            if ivar < nargs:
                a += [ s ]
            else:
                a += [var + '=' + s]
        name = self.__name__ if hasattr(self, '__name__') else \
               type(self).__name__ + '[not initialized]'
        return name + '(' + ', '.join(a) + ')'


def DirectOperatorFactory(cls, source, *args, **keywords):
    return cls(attrin=source.attrin, attrout=source.attrout,
               classin=source.classin, classout=source.classout,
               commin=source.commin, commout=source.commout,
               reshapein=source.reshapein, reshapeout=source.reshapeout,
               shapein=source.shapein, shapeout=source.shapeout,
               toshapein=source.toshapein, toshapeout=source.toshapeout,
               validatein=source.validatein, validateout=source.validateout,
               dtype=source.dtype, flags=source.flags, *args, **keywords)


def ReverseOperatorFactory(cls, source, *args, **keywords):
    return cls(attrin=source.attrout, attrout=source.attrin,
               classin=source.classout, classout=source.classin,
               commin=source.commout, commout=source.commin,
               reshapein=source.reshapeout, reshapeout=source.reshapein,
               shapein=source.shapeout, shapeout=source.shapein,
               toshapein=source.toshapeout, toshapeout=source.toshapein,
               validatein=source.validateout, validateout=source.validatein,
               dtype=source.dtype, flags=source.flags, *args, **keywords)


def asoperator(operator, shapein=None, shapeout=None):
    if isinstance(operator, Operator):
        if shapein and operator.shapein and shapein != operator.shapein:
            raise ValueError('The input shapein ' + str(shapein) + ' is incompa'
                'atible with that of the input ' + str(operator.shapein) + '.')
        if shapeout and operator.shapeout and shapeout != operator.shapeout:
            raise ValueError('The input shapeout ' + str(shapeout) + ' is incom'
                'patible with that of the input ' + str(operator.shapeout) +  \
                '.')
        if shapein and not operator.shapein or \
           shapeout and not operator.shapeout:
            operator = copy.copy(operator)
            operator.shapein = shapein
            operator.shapeout = shapeout
        return operator

    if hasattr(operator, 'matvec') and hasattr(operator, 'rmatvec') and \
       hasattr(operator, 'shape'):
        def direct(input, output):
            output[...] = operator.matvec(input)
        def transpose(input, output):
            output[...] = operator.rmatvec(input)
        return Operator(direct=direct,
                        transpose=transpose,
                        shapein=shapein or operator.shape[1],
                        shapeout=shapeout or operator.shape[0],
                        dtype=operator.dtype,
                        flags='linear')
    
    if isscalar(operator):
        return HomothetyOperator(operator)

    return asoperator(scipy.sparse.linalg.aslinearoperator(operator))


def asoperator1d(operator):
    operator = asoperator(operator)
    r = ReshapeOperator(operator.shape[1], operator.shapein)
    s = ReshapeOperator(operator.shapeout, operator.shape[0])
    return s * operator * r


class CompositeOperator(Operator):
    """
    Abstract class for grouping operands.

    """
    def __new__(cls, operands=None, *args, **keywords):
        if operands is not None:
            operands = cls._validate_operands(operands)
            operands = cls._apply_rules(operands)
            if len(operands) == 1:
                return operands[0]
            cls._validate_comm(operands)
        instance = super(CompositeOperator, cls).__new__(cls)
        instance.operands = operands
        return instance

    def __init__(self, operands=None, *args, **keywords):
        if operands is None:
            return
        dtype = self._find_common_type([o.dtype for o in self.operands])
        Operator.__init__(self, dtype=dtype, **keywords)
        self.propagate_commin(self.commin)
        self.propagate_commout(self.commout)

    def propagate_attributes(self, cls, attr):
        return self.operands[0].propagate_attributes(cls, attr)
            
    def propagate_commin(self, commin):
        if commin is None:
            return self
        self.commin = commin
        for i, op in enumerate(self.operands):
           self.operands[i] = op.propagate_commin(commin)
        return self

    def propagate_commout(self, commout):
        if commout is None:
            return self
        self.commout = commout
        for i, op in enumerate(self.operands):
           self.operands[i] = op.propagate_commout(commout)
        return self

    @classmethod
    def _apply_rules(cls, ops):
        return ops

    @classmethod
    def _validate_operands(cls, operands):
        if isinstance(operands, Operator):
            operands = [operands]
        operands = [asoperator(op) for op in operands]
        result = []
        for op in operands:
            if isinstance(op, cls):
                result.extend(op.operands)
            else:
                result.append(op)
        return result

    @classmethod
    def _validate_comm(cls, operands):
        comms = [op.commin for op in operands if op.commin is not None]
        if len(set(id(c) for c in comms)) > 1:
            raise ValueError('The input MPI communicators are incompatible.')
        comms = [op.commout for op in operands if op.commout is not None]
        if len(set(id(c) for c in comms)) > 1:
            raise ValueError('The output MPI communicators are incompatible.')
        return operands

    def __str__(self):
        if isinstance(self, AdditionOperator):
            op = ' + '
        elif isinstance(self, (BlockDiagonalOperator, BlockSliceOperator)):
            op = ' ⊕ '
        else:
            op = ' * '

        # parentheses for AdditionOperator and BlockDiagonalOperator
        operands = ['({0})'.format(o) if isinstance(o, (AdditionOperator,
                    BlockDiagonalOperator)) else str(o) for o in self.operands]

        # some special cases
        if isinstance(self, BlockDiagonalOperator) and  len(operands) > 2:
            operands = [operands[0], '...', operands[-1]]
        elif isinstance(self, CompositionOperator) and \
           isinstance(self.operands[0], HomothetyOperator):
            # remove trailing 'I'
            operands[0] = operands[0][:-1]
            if self.operands[0].data == -1:
                operands[0] += '1'

        return op.join(operands)

    def __repr__(self):
        r = self.__name__ + '(['
        rops = [repr(op) for op in self.operands]
        components = []
        for i, rop in enumerate(rops):
            if i != len(rops) - 1:
                rop += ','
            components.extend(rop.split('\n'))
        r += '\n    '+'\n    '.join(components) + '])'
        return r


class CommutativeCompositeOperator(CompositeOperator):
    """
    Abstract class for commutative composite operators, such as the addition or
    the element-wise multiplication.
    """
    def __new__(cls, operands=None, operation=None, *args, **keywords):
        if operands is not None:
            operands = cls._validate_constants(operands)
        return CompositeOperator.__new__(cls, operands, *args, **keywords)

    def __init__(self, operands=None, operation=None, *args, **keywords):
        if operands is None:
            return
        classin = first_is_not([o.classin for o in self.operands], None)
        classout = first_is_not([o.classout for o in self.operands], None)
        commin = first_is_not([o.commin for o in self.operands], None)
        commout = first_is_not([o.commout for o in self.operands], None)
        CompositeOperator.__init__(self, operands, commin=commin, commout= \
            commout, classin=classin, classout=classout, *args, **keywords)
        self.operation = operation

    def direct(self, input, output):
        operands = list(self.operands)
        assert len(operands) > 1

        try:
            ir = [o.flags.inplace_reduction for o in operands]
            index = ir.index(False)
            operands[0], operands[index] = operands[index], operands[0]
            need_temporary = ir.count(False) > 1
        except ValueError:
            need_temporary = False

        if need_temporary:
            memory.up()
            buf = memory.get(output.nbytes, output.shape, output.dtype,
                      self.__name__).view(output.dtype).reshape(output.shape)

        operands[0].direct(input, output)

        for op in operands[1:]:
            if op.flags.inplace_reduction:
                op.direct(input, output, operation=self.operation)
            else:
                op.direct(input, buf)
                self.operation(output, buf)

        if need_temporary:
            memory.down()

    def propagate_attributes(self, cls, attr):
        for op in self.operands:
            cls = op.propagate_attributes(cls, attr)
        return cls

    def validatereshapein(self, shapein):
        shapeout = super(CommutativeCompositeOperator, self).validatereshapein(
                         shapein)
        if shapeout is not None:
            return shapeout
        for op in self.operands:
            shapeout_ = op.validatereshapein(shapein)
            if shapeout_ is None:
                continue
            if shapeout is None:
                shapeout = shapeout_
                continue
            if shapeout != shapeout_:
                raise ValueError("Incompatible shape in operands: '{0}' and '{1"
                                 "}'.".format(shapeout, shapeout_))
        return shapeout

    def validatereshapeout(self, shapeout):
        shapein = super(CommutativeCompositeOperator, self).validatereshapeout(shapeout)
        if shapein is not None:
            return shapein
        for op in self.operands:
            shapein_ = op.validatereshapeout(shapeout)
            if shapein_ is None:
                continue
            if shapein is None:
                shapein = shapein_
                continue
            if shapein != shapein_:
                raise ValueError("Incompatible shape in operands: '{0}' and '{1"
                                 "}'.".format(shapein, shapein_))
        return shapein

    @classmethod
    def _apply_rules(cls, ops):
        if len(ops) <= 1:
            return ops
        i = 0
        while i < len(ops):
            if cls not in ops[i].rules:
                i += 1
                continue
            j = 0
            consumed = False
            while j < len(ops):
                if j != i:
                    for rule in ops[i].rules[cls]:
                        new_ops = rule(ops[i], ops[j])
                        if new_ops is None:
                            continue
                        del ops[j]
                        if j < i:
                            i -= 1
                        ops[i] = new_ops
                        consumed = True
                        break
                    if consumed:
                        break
                if consumed:
                    break
                j += 1
            if consumed:
                continue
            i += 1

        # move this up to avoid creations of temporaries
        i = [i for i,o in enumerate(ops) if isinstance(o, HomothetyOperator)]
        if len(i) > 0:
            ops.insert(0, ops[i[0]])
            del ops[i[0]+1]
            if ops[0].data == 0 and len(ops) > 1:
                del ops[0]
        return ops

    @classmethod
    def _validate_constants(cls, operands):
        """ Convert constants into ConstantOperator. """
        for i, op in enumerate(operands):
            if isinstance(op, (int, float, complex, np.bool_, np.number,
                          np.ndarray)):
                operands[i] = ConstantOperator(op)
        return operands


class AdditionOperator(CommutativeCompositeOperator):
    """
    Class for operator addition

    If at least one of the input already is the result of an addition,
    a flattened list of operators is created by associativity, to simplify
    reduction.
    """
    def __new__(cls, operands=None):
        return CommutativeCompositeOperator.__new__(cls, operands,
                                                    operator.iadd)

    def __init__(self, operands=None):
        if operands is None:
            return
        flags = {
            'linear':all([op.flags.linear for op in self.operands]),
            'real':all([op.flags.real for op in self.operands]),
            'square':all([op.flags.square for op in self.operands]),
            'symmetric':all([op.flags.symmetric for op in self.operands]),
            'hermitian':all([op.flags.hermitian for op in self.operands]),
            'universal':all([op.flags.universal for op in self.operands])}
        CommutativeCompositeOperator.__init__(self, operands, operator.iadd,
                                              flags=flags)
        self.set_rule('.T', lambda s: type(s)([m.T for m in s.operands]))
        self.set_rule('.H', lambda s: type(s)([m.H for m in s.operands]))
        self.set_rule('.C', lambda s: type(s)([m.C for m in s.operands]))
                

class MultiplicationOperator(CommutativeCompositeOperator):
    """
    Class for Hadamard (element-wise) multiplication of operators.

    If at least one of the input already is the result of an multiplication,
    a flattened list of operators is created by associativity, to simplify
    reduction.
    """
    def __new__(cls, operands=None):
        return CommutativeCompositeOperator.__new__(cls, operands,
                                                    operator.imul)

    def __init__(self, operands=None):
        if operands is None:
            return
        flags = {
            'real':all([op.flags.real for op in self.operands]),
            'square':all([op.flags.square for op in self.operands]),
            'universal':all([op.flags.universal for op in self.operands])}
        CommutativeCompositeOperator.__init__(self, operands, operator.imul,
                                              flags=flags)
        self.set_rule('.C', lambda s: type(s)([m.C for m in s.operands]))


@square
class BlockSliceOperator(CommutativeCompositeOperator):
    """
    Class for multiple disjoint slices.

    The elements of the input not included in the slices are copied over
    to the output. This is due to fact that is not easy to derive the complement
    of a set of slices. To set those values to zeros, you might use MaskOperator
    or write a custom operator.
    Currently, there is no check to verify that the slices are disjoint.
    Non-disjoint slices can lead to unexpected results.

    Examples
    --------
    >>> op = BlockSliceOperator(HomothetyOperator(3), slice(None,None,2))
    >>> op(np.ones(6))
    array([ 3.,  1.,  3.,  1.,  3.,  1.])

    >>> op = BlockSliceOperator([ConstantOperator(1), ConstantOperator(2)],
                                ([slice(0,2), slice(0,2)], 
                                 [slice(2,4), slice(2,4)]))
    >>> op(np.zeros((4,4)))
    array([[ 1.,  1.,  0.,  0.],
           [ 1.,  1.,  0.,  0.],
           [ 0.,  0.,  2.,  2.],
           [ 0.,  0.,  2.,  2.]])

    """
    def __init__(self, operands, slices, **keywords):
        if any(not op.flags.square and op.flags.shape_output != 'unconstrained'
               for op in self.operands):
            raise ValueError('Input operands must be square.')
        if not isinstance(slices, (list, tuple, slice)):
            raise TypeError('Invalid input slices.')
        if isinstance(slices, slice):
            slices = (slices,)
        if len(self.operands) != len(slices):
            raise ValueError("The number of slices '{0}' is not equal to the nu"
                "mber of operands '{1}'.".format(len(slices), len(self.operands)
                ))

        flags = {
            'linear':all(op.flags.linear for op in self.operands),
            'real':all(op.flags.real for op in self.operands),
            'symmetric':all(op.flags.symmetric for op in self.operands),
            'inplace':all(op.flags.inplace for op in self.operands),
        }

        CommutativeCompositeOperator.__init__(self, operands, flags=flags)
        self.slices = tuple(slices)
        self.set_rule('.C', lambda s: 
                      BlockSliceOperator([op.C for op in s.operands], s.slices))
        self.set_rule('.T', lambda s: 
                      BlockSliceOperator([op.T for op in s.operands], s.slices))
        self.set_rule('.H', lambda s: 
                      BlockSliceOperator([op.H for op in s.operands], s.slices))

    def direct(self, input, output):
        if not self.same_data(input, output):
            output[...] = input
        memory.up()
        for s, op in zip(self.slices, self.operands):
            o = output[s]
            with memory.push_and_pop(o):
                op.direct(input[s], o)
        memory.down()

    def validatereshapein(self, shapein):
        return super(CompositeOperator, self).validatereshapein(shapein)

    def validatereshapeout(self, shapeout):
        return super(CompositeOperator, self).validatereshapeout(shapeout)


class NonCommutativeCompositeOperator(CompositeOperator):
    """
    Abstract class for non-commutative composite operators, such as
    the composition.
    """
    @classmethod
    def _apply_rules(cls, ops):
        if len(ops) <= 1:
            return ops
        i = len(ops) - 2

        # loop over the len(ops)-1 pairs of operands
        while i >= 0:
            
            o1 = ops[i]
            o2 = ops[i+1]
            rules1 = o1.rules[cls]['left'] if cls in o1.rules else []
            rules2 = o2.rules[cls]['right'] if cls in o2.rules else []

            # subclasses rules have a higher priority than those of superclasses
            if cls._ge_operator(o1, o2):
                rules = rules1 + rules2
            else:
                rules = rules2 + rules1

            consumed = False
            for rule in rules:
                new_ops = rule(o1, o2)
                if new_ops is None:
                    continue
                consumed = True
                if isinstance(new_ops, tuple):
                    if len(new_ops) != 2:
                        raise NotImplementedError()
                    ops[i], ops[i+1] = new_ops
                    break
                if rule.merge:
                    cls._merge(new_ops, o1, o2)
                del ops[i+1]
                ops[i] = new_ops
                break

            if consumed and i < len(ops) - 1:
                continue

            i -= 1

        return ops

    @staticmethod
    def _ge_operator(o1, o2):
        """
        Return true if the first operator has a higher priority, i.e. if it
        subclasses the second argument class.
        """
        t1 = type(o1)
        t2 = type(o2)
        return issubclass(t1, t2) and t1 is not t2


@inplace
class CompositionOperator(NonCommutativeCompositeOperator):
    """
    Class handling operator composition.

    If at least one of the input already is the result of a composition,
    a flattened list of operators is created by associativity, to simplify
    reduction.
    """

    def __init__(self, operands=None):
        flags = self._merge_flags(self.operands)
        classin = first_is_not([o.classin for o in self.operands[::-1]], None)
        classout = first_is_not([o.classout for o in self.operands], None)
        commin = first_is_not([o.commin for o in self.operands[::-1]], None)
        commout = first_is_not([o.commout for o in self.operands], None)
        NonCommutativeCompositeOperator.__init__(self, operands, flags=flags,
            classin=classin, classout=classout, commin=commin, commout=commout)

        self._info = {}
        self.set_rule('.C', lambda s:type(s)([m.C for m in s.operands]))
        self.set_rule('.T', lambda s:type(s)([m.T for m in s.operands[::-1]]))
        self.set_rule('.H', lambda s:type(s)([m.H for m in s.operands[::-1]]))
        self.set_rule('.I', lambda s:type(s)([m.I for m in s.operands[::-1]]))
        self.set_rule('.IC',lambda s:type(s)([m.I.C for m in s.operands[::-1]]))
        self.set_rule('.IT', lambda s:type(s)([m.I.T for m in s.operands]))
        self.set_rule('.IH', lambda s:type(s)([m.I.H for m in s.operands]))

    def direct(self, input, output, operation=operation_assignment):

        inplace_composition = self.same_data(input, output)
        shapeouts, sizeouts, outplaces, reuse_output = self._get_info(
            input.shape, output.shape, output.dtype, inplace_composition and \
            operation is operation_assignment)
        noutplaces = outplaces.count(True)

        nswaps = 0
        if not reuse_output:
            memory.up()
        elif inplace_composition and outplaces[-1] or \
             not inplace_composition and noutplaces % 2 == 0:
            memory.swap()
            nswaps += 1

        i = input
        for iop, (op, shapeout, sizeout, outplace) in enumerate(
            zip(self.operands, shapeouts, sizeouts, outplaces)[:0:-1]):
            if outplace and iop > 0:
                memory.up()
                o = memory.get(sizeout, shapeout, output.dtype, self.__name__) \
                          .view(output.dtype).reshape(shapeout)
                op.direct(i, o)
                i = o
                memory.down()
                memory.swap()
                nswaps += 1
            else:
                # we keep reusing the same stack element for inplace operators
                o = memory.get(sizeout, shapeout, output.dtype, self.__name__) \
                          .view(output.dtype).reshape(shapeout)
                op.direct(i, o)
                i = o

        if outplaces[0]:
            memory.up()
        if self.flags.inplace_reduction:
            self.operands[0].direct(i, output, operation=operation)
        else:
            self.operands[0].direct(i, output)
        if outplaces[0]:
            memory.down()
            memory.swap()
            nswaps += 1
            
        if nswaps % 2 == 1:
            memory.swap()

        if not reuse_output:
            memory.down()

    def propagate_attributes(self, cls, attr):
        for op in reversed(self.operands):
            cls = op.propagate_attributes(cls, attr)
        return cls

    def propagate_commin(self, commin):
        if commin is None:
            return self
        self.commin = commin
        for i, op in reversed(list(enumerate(self.operands))):
            if op.commin is not None:
                commin = op.commout
            else:
                op = op.propagate_commin(commin)
                self.operands[i] = op
                commin = op.commout or commin
        return self

    def propagate_commout(self, commout):
        if commout is None:
            return self
        self.commout = commout
        for i, op in enumerate(self.operands):
            if op.commout is not None:
                commout = op.commin
            else:
                op = op.propagate_commout(commout)
                self.operands[i] = op
                commout = op.commin or commout
        return self

    def validatereshapein(self, shapein):
        shapeout = super(CompositionOperator, self).validatereshapein(shapein)
        if shapeout is not None:
            return shapeout
        return self._get_shapes(shapein, None, self.operands)[0]

    def validatereshapeout(self, shapeout):
        shapein = super(CompositionOperator, self).validatereshapeout(shapeout)
        if shapein is not None:
            return shapein
        return self._get_shapes(None, shapeout, self.operands)[-1]

    def _get_info(self, shapein, shapeout, dtype, inplace):
        try:
            return self._info[(shapein, shapeout, dtype, inplace)]
        except KeyError:
            pass
        shapeouts = self._get_shapes(shapein, shapeout, self.operands)[:-1]
        if None in shapeouts:
            raise ValueError("The composition of an unconstrained input shape o"
                             "perator by an unconstrained output shape operator"
                             " is ambiguous.")
        sizeouts = self._get_sizeouts(shapeouts)
        nbytes = reduce(lambda x,y:x*y, shapeout, 1) * dtype.itemsize
        outplaces, reuse_output = self._get_outplaces(nbytes, inplace, sizeouts)
        v = shapeouts, sizeouts, outplaces, reuse_output
        self._info[(shapein,shapeout,dtype,inplace)] = v
        return v

    @staticmethod
    def _get_shapes(shapein, shapeout, operands):
        """
        Return the output, intermediate and input shapes of the composed
        operands as a list.
        """
        n = len(operands)
        shapes = [shapeout] + (n - 1) * [None] + [shapein]

        # scanning from the innermost to the outermost operand
        for i in range(n-1, -1, -1):
            op = operands[i]
            s = op.validatereshapein(shapes[i+1])
            if i == 0 and None not in (shapes[0], s) and s != shapes[0]:
                raise ValueError("Incompatible shape in composition.")
            if s is not None:
                shapes[i] = s
        
        # scanning from the outermost to the innermost operand
        for i in range(n):
            op = operands[i]
            s = op.validatereshapeout(shapes[i])
            if None not in (shapes[i+1], s) and s != shapes[i+1]:
                raise ValueError("Incompatible shape in composition.")
            if s is not None:
                shapes[i+1] = s

        return shapes

    def _get_sizeouts(self, shapeouts):
        # assuming input's dtype is float64
        sizeouts = []
        dtype = np.dtype(np.float64)
        for op, shapeout in reversed(zip(self.operands, shapeouts)):
            dtype = self._find_common_type([dtype, op.dtype])
            sizeouts.insert(0, dtype.itemsize * np.prod(shapeout))
        return sizeouts

    def _get_outplaces(self, output_nbytes, inplace_composition, sizeouts):
        outplaces = [not op.flags.inplace for op in self.operands]
        if not inplace_composition:
            outplaces[-1] = True

        noutplaces = outplaces.count(True)
        if inplace_composition and noutplaces % 2 == 1 and \
           noutplaces == len(self.operands):
            return outplaces, False

        last_inplace_changed_to_outplace = False
        if inplace_composition:
            # if composition is inplace, enforce  even number of outplace
            if noutplaces % 2 == 1 and False in outplaces:
                index = outplaces.index(False)
                outplaces[index] = True
                last_inplace_changed_to_outplace = True
            output_is_requested = True # we start with the input=output
        else:
            output_is_requested = noutplaces % 2 == 0

        reuse_output = False
        for op, outplace, nbytes in zip(self.operands, outplaces,
                                        sizeouts)[:0:-1]:
            if outplace:
                output_is_requested = not output_is_requested
            if output_is_requested:
                if nbytes > output_nbytes:
                    if last_inplace_changed_to_outplace:
                        outplaces[index] = False # revert back
                    return outplaces, False
                reuse_output = True
        return outplaces, reuse_output

    @classmethod
    def _merge(cls, op, op1, op2):
        """
        Ensure that op = op1*op2 has a correct shapein, shapeout, etc.
        """
        attrout = cls._merge_attr(op1.attrout, op2.attrout)
        attrin = cls._merge_attr(op2.attrin, op1.attrin)
        classout = op1.classout or op2.classout
        classin = op2.classin or op1.classin
        commout = op1.commout or op2.commout
        commin = op2.commin or op1.commin
        dtype = cls._find_common_type([op1.dtype, op2.dtype])
        flags = cls._merge_flags([op1, op2])
        shapes = cls._get_shapes(op2.shapein, op1.shapeout, [op1, op2])
        shapein = shapes[-1]
        shapeout = shapes[0]
        reshapein = cls._mergereshapein(op1, op2)
        reshapeout = cls._mergereshapeout(op1, op2)
        toshapein = op2.toshapein
        toshapeout = op1.toshapeout
        validatein = op2.validatein
        validateout = op1.validateout
        op._C = op._T = op._H = op._I = None
        op._init_dtype(dtype)
        op._init_flags(flags)
        op._init_inout(attrin, attrout, classin, classout, commin, commout,
                       reshapein, reshapeout, shapein, shapeout, toshapein,
                       toshapeout, validatein, validateout)

    @staticmethod
    def _merge_attr(attr1, attr2):
        if None in (attr1, attr2):
            return attr1 or attr2
        if isinstance(attr1, dict) and isinstance(attr2, dict):
            attr = attr2.copy()
            attr.update(attr1)
            return attr
        if isinstance(attr1, dict):
            def func(attr):
                attr2(attr)
                attr.update(attr1)
        elif isinstance(attr2, dict):
            def func(attr):
                attr.update(attr2)
                attr1(attr)
        else:
            def func(attr):
                attr2(attr)
                attr1(attr)
        return func

    @staticmethod
    def _merge_flags(ops):
        flags = {
            'linear':all([op.flags.linear for op in ops]),
            'real':all([op.flags.real for op in ops]),
            'square':all([op.flags.square for op in ops]),
            'universal':all([op.flags.universal for op in ops]),
            'inplace_reduction':ops[0].flags.inplace_reduction,
        }
        return flags

    @staticmethod
    def _mergereshapein(op1, op2):
        if any(o.flags.shape_output != 'implicit' for o in [op1,op2]):
            return None
        if op1.flags.square and op2.flags.square:
            return op1.validatereshapein
        def reshapein(shapein):
            return op1.reshapein(op2.reshapein(shapein))
        return reshapein

    @staticmethod
    def _mergereshapeout(op1, op2):
        if any(o.flags.shape_input != 'implicit' for o in [op1,op2]):
            return None
        if op1.flags.square and op2.flags.square:
            return op1.reshapeout
        def reshapeout(shape):
            return op2.reshapeout(op1.reshapeout(shape))
        return reshapeout

    @classmethod
    def _validate_comm(cls, operands):
        for op1, op2 in zip(operands[:-1], operands[1:]):
            commin = op1.commin
            commout = op2.commout
            if None not in (commin, commout) and commin is not commout:
                raise ValueError('The MPI communicators are incompatible.')
        return operands


class BlockOperator(NonCommutativeCompositeOperator):
    """
    Abstract base class for BlockDiagonalOperator, BlockColumnOperator and
    BlockRowOperator.
    """

    def __init__(self, operands, partitionin=None, partitionout=None,
                 axisin=None, axisout=None, new_axisin=None, new_axisout=None):

        if new_axisin is not None:
            if partitionin is None:
                partitionin = len(self.operands) * (1,)
            elif any(p not in (None, 1) for p in partitionin):
                raise ValueError('If the block operator input shape has one mor'
                                 'e dimension than its blocks, the input partit'
                                 'ion must be a tuple of ones.')
        if new_axisout is not None:
            if partitionout is None:
                partitionout = len(self.operands) * (1,)
            elif any(p not in (None, 1) for p in partitionout):
                raise ValueError('If the block operator output shape has one mo'
                                 're dimension than its blocks, the output part'
                                 'ition must be a tuple of ones.')

        if axisin is not None and new_axisin is not None:
            raise ValueError("The keywords 'axisin' and 'new_axisin' are exclus"
                             "ive.")
        if axisout is not None and new_axisout is not None:
            raise ValueError("The keywords 'axisout' and 'new_axisout' are excl"
                             "usive.")

        if partitionin is partitionout is None:
            raise ValueError('No partition is provided.')
        if partitionin is not None:
            if len(partitionin) != len(self.operands):
                raise ValueError('The number of operators must be the same as t'
                                 'he length of the input partition.')
            partitionin = merge_none(partitionin, self._get_partitionin(
                                      operands, partitionout, axisin, axisout,
                                      new_axisin, new_axisout))
        if partitionout is not None:
            if len(partitionout) != len(self.operands):
                raise ValueError('The number of operators must be the same as t'
                                 'he length of the output partition.')
            partitionout = merge_none(partitionout, self._get_partitionout(
                                      operands, partitionin, axisin, axisout,
                                      new_axisin, new_axisout))

        flags = {
            'linear':all(op.flags.linear for op in self.operands),
            'real':all(op.flags.real for op in self.operands)
        }

        if partitionin is not None and partitionout is not None:
            flags['square'] = all(op.flags.square for op in self.operands)
            flags['symmetric'] = all(op.flags.symmetric for op in self.operands)
            flags['hermitian'] = all(op.flags.hermitian for op in self.operands)

        self.partitionin = tointtuple(partitionin)
        self.partitionout = tointtuple(partitionout)
        self.axisin = axisin
        self.new_axisin = new_axisin
        self.axisout = axisout
        self.new_axisout = new_axisout
        if partitionin is None:
            self.__class__ = BlockColumnOperator
        elif partitionout is None:
            self.__class__ = BlockRowOperator
        else:
            self.__class__ = BlockDiagonalOperator
        commin = first_is_not([o.commin for o in self.operands], None)
        commout = first_is_not([o.commout for o in self.operands], None)
        CompositeOperator.__init__(self, operands, commin=commin,
                                   commout=commout, flags=flags)

        if self.shapein is not None:
            n = len(self.shapein)
            if self.axisin is not None and self.axisin < 0:
                self.axisin += n
            elif self.new_axisin is not None and self.new_axisin < 0:
                self.new_axisin += n
        if self.shapeout is not None:
            n = len(self.shapeout)
            if self.axisout is not None and self.axisout < 0:
                self.axisout += n
            elif self.new_axisout is not None and self.new_axisout < 0:
                self.new_axisout += n

        self.set_rule('.C', lambda s: BlockOperator([op.C for op in s.operands],
                      s.partitionin, s.partitionout, s.axisin, s.axisout,
                      s.new_axisin, s.new_axisout))
        self.set_rule('.T', lambda s: BlockOperator([op.T for op in s.operands],
                      s.partitionout, s.partitionin, s.axisout, s.axisin,
                      s.new_axisout, s.new_axisin))
        self.set_rule('.H', lambda s: BlockOperator([op.H for op in s.operands],
                      s.partitionout, s.partitionin, s.axisout, s.axisin,
                      s.new_axisout, s.new_axisin))

        if isinstance(self, BlockDiagonalOperator):
            self.set_rule('.I', lambda s: type(s)([op.I for op in
                      s.operands], s.partitionout, s.axisout, s.axisin,
                      s.new_axisout, s.new_axisin))
            self.set_rule('.IC', lambda s: type(s)([op.I.C for op in \
                      s.operands], s.partitionout, s.axisout, s.axisin,
                      s.new_axisout, s.new_axisin))
            self.set_rule('.IT', lambda s: type(s)([op.I.T for op in \
                      s.operands], s.partitionin, s.axisin, s.axisout,
                      s.new_axisin, s.new_axisout))
            self.set_rule('.IH', lambda s: type(s)([o.I.H for o in \
                      s.operands], s.partitionin, s.axisin, s.axisout,
                      s.new_axisin, s.new_axisout))

        self.set_rule('.{Operator}', self._rule_add_operator, AdditionOperator)
        self.set_rule('.{Operator}', self._rule_left_operator,
                      CompositionOperator)
        self.set_rule('{Operator}.', self._rule_right_operator,
                      CompositionOperator)
        self.set_rule('{self}.', self._rule_add_blockoperator,
                      AdditionOperator, merge=False)
        self.set_rule('{self}.', self._rule_mul_blockoperator,
                      MultiplicationOperator, merge=False)
        self.set_rule('{BlockOperator}.', self._rule_comp_blockoperator,
                      CompositionOperator, merge=False)

    def validatereshapein(self, shapein):
        shapeout = super(BlockOperator, self).validatereshapein(shapein)
        if shapeout is not None:
            return shapeout

        if shapein is None or self.partitionin is None:
            shapeouts = [op.validatereshapein(shapein) for op in self.operands]
        else:
            shapeouts = [op.validatereshapein(s) for op,s in zip(self.operands,
                self._get_shapes(shapein, self.partitionin, self.axisin,
                self.new_axisin))]
        if None in shapeouts:
            return None
        shapeout = self._validate_shapes(shapeouts, self.partitionout,
                                         self.axisout, self.new_axisout)
        if shapeout is None:
            return None
        if self.partitionout is None:
            return shapeout
        if self.new_axisout is not None:
            a = self.new_axisout
            if self.new_axisout < 0:
                a += len(shapeout) + 1
            return shapeout[:a] + (len(self.operands),) + shapeout[a:]
        shapeout = list(shapeout)
        shapeout[self.axisout] = sum([s[self.axisout] for s in shapeouts])
        return tointtuple(shapeout)

    def validatereshapeout(self, shapeout):
        shapein = super(BlockOperator, self).validatereshapeout(shapeout)
        if shapein is not None:
            return shapein

        if shapeout is None or self.partitionout is None:
            shapeins = [op.validatereshapeout(shapeout) for op in self.operands]
        else:
            shapeins = [op.validatereshapeout(s) for op,s in zip(self.operands,
                self._get_shapes(shapeout, self.partitionout, self.axisout,
                self.new_axisout))]
        if None in shapeins:
            return None
        shapein = self._validate_shapes(shapeins, self.partitionin,
                                         self.axisin, self.new_axisin)
        if shapein is None:
            return None
        if self.partitionin is None:
            return shapein
        if self.new_axisin is not None:
            a = self.new_axisin
            if self.new_axisin < 0:
                a += len(shapein) + 1
            return shapein[:a] + (len(self.operands),) + shapein[a:]
        shapein = list(shapein)
        shapein[self.axisin] = sum([s[self.axisin] for s in shapeins])
        return tointtuple(shapein)

    def toshapein(self, v):
        if self.shapein is not None:
            return v.reshape(self.shapein)
        if self.partitionin is None:
            return self.operands[0].toshapein(v)
        axisin = self.axisin if self.axisin is not None else self.new_axisin
        if None in self.partitionin or axisin not in (0,-1):
            raise ValueError('Ambiguous reshaping.')
        p = sum(self.partitionin)
        if v.size == p:
            return v
        if axisin == 0:
            return v.reshape((p,-1))
        return v.reshape((-1,p))
            
    def toshapeout(self, v):
        if self.shapeout is not None:
            return v.reshape(self.shapeout)
        if self.partitionout is None:
            return self.operands[0].toshapeout(v)
        axisout = self.axisout if self.axisout is not None else self.new_axisout
        if None in self.partitionout or axisout not in (0,-1):
            raise ValueError('Ambiguous reshaping.')
        p = sum(self.partitionout)
        if v.size == p:
            return v
        if axisout == 0:
            return v.reshape((p,-1))
        return v.reshape((-1,p))

    @staticmethod
    def _get_partition(shapes, axis, new_axis):
        if new_axis is not None:
            return len(shapes) * (1,)
        return tuple(None if s is None else s[axis] for s in shapes)

    @staticmethod
    def _get_partitionin(ops, partitionout, axisin, axisout, new_axisin,
                          new_axisout):
        """ Infer the input partition from the output partition. """
        if new_axisin is not None:
            return len(ops) * (1,)

        if partitionout is None:
            return [o.shapein[axisin] if o.shapein else None for o in ops]

        if new_axisout is None:
            ndim_min = axisout+1 if axisout >= 0 else -axisout
        else:
            ndim_min = 0
        partitionin = len(ops) * [None]
        for i, op in enumerate(ops):
            if op.shapein is not None:
                partitionin[i] = op.shapein[axisin]
                continue
            if partitionout[i] is None:
                continue
            pin = []
            # for implicit input shape operators, we should make sure that
            # partitionin does not depend on the rank of the output
            for ndim in range(ndim_min, 33):
                shapeout = ndim * [0]
                if new_axisout is None:
                    shapeout[axisout] = partitionout[i]
                try:
                    shapein = op.validatereshapeout(shapeout)
                    pin.append(shapein[axisin])
                except IndexError:
                    continue
            if len(pin) == 0 or any([p != pin[0] for p in pin]):
                continue
            partitionin[i] = pin[0]
        return tuple(partitionin)

    @staticmethod
    def _get_partitionout(ops, partitionin, axisin, axisout, new_axisin,
                          new_axisout):
        """ Infer the output partition from the input partition. """
        if new_axisout is not None:
            return len(ops) * (1,)

        if partitionin is None:
            return [o.shapeout[axisout] if o.shapeout else None for o in ops]

        if new_axisin is None:
            ndim_min = axisin+1 if axisin >= 0 else -axisin
        else:
            ndim_min = 0
        partitionout = len(ops) * [None]
        for i, op in enumerate(ops):
            if op.shapeout is not None:
                partitionout[i] = op.shapeout[axisout]
                continue
            if partitionin[i] is None:
                continue
            pout = []
            # for implicit output shape operators, we should make sure that
            # partitionout does not depend on the rank of the input
            for ndim in range(ndim_min, 33):
                shapein = ndim * [0]
                if new_axisin is None:
                    shapein[axisin] = partitionin[i]
                try:
                    shapeout = op.validatereshapein(shapein)
                    pout.append(shapeout[axisout])
                except IndexError:
                    continue
            if len(pout) == 0 or any([p != pout[0] for p in pout]):
                continue
            partitionout[i] = pout[0]
        return tuple(partitionout)

    @staticmethod
    def _get_shapes(shape, partition, axis, new_axis):
        if None in partition:
            return len(partition) * (None,)
        if new_axis is not None:
            shape_ = list(shape)
            del shape_[new_axis]
            shapes = len(partition) * (tuple(shape_),)
            return shapes
        shapes = []
        for p in partition:
            shape_ = list(shape)
            shape_[axis] = p
            shapes.append(shape_)
        return tuple(shapes)

    @staticmethod
    def _get_slice(axis, new_axis):
        """ Compute the tuple of slices to extract a block from the input. """
        axis = axis if axis is not None else new_axis
        if axis is None:
            return None
        if axis >= 0:
            return (axis+1) * [slice(None)] + [Ellipsis]
        return [Ellipsis] + (-axis) * [slice(None)]

    @staticmethod
    def _get_slices(partition, axis, new_axis):
        """ Return iterator of the block slices. """
        s = BlockOperator._get_slice(axis, new_axis)
        dest = 0
        for n in partition:
            if new_axis is not None:
                s[new_axis] = dest
            else:
                s[axis] = slice(dest, dest + n)
            dest += n
            yield list(s)

    def get_slicesin(self, partitionin=None):
        """ Return iterator of the block input slices. """
        if partitionin is None:
            partitionin = self.partitionin
        return self._get_slices(partitionin, self.axisin, self.new_axisin)
 
    def get_slicesout(self, partitionout=None):
        """ Return iterator of the block output slices. """
        if partitionout is None:
            partitionout = self.partitionout
        return self._get_slices(partitionout, self.axisout, self.new_axisout)

    @staticmethod
    def _validate_commutative(op1, op2):
        axisin1 = op1.axisin if op1.axisin is not None else op1.new_axisin
        axisin2 = op2.axisin if op2.axisin is not None else op2.new_axisin
        axisout1 = op1.axisout if op1.axisout is not None else op1.new_axisout
        axisout2 = op2.axisout if op2.axisout is not None else op2.new_axisout
        if axisin1 != axisin2 or axisout1 != axisout2:
            return None
        if op1.axisin is not None and op2.new_axisin is not None or \
           op1.new_axisin is not None and op2.axisin is not None or \
           op1.axisout is not None and op2.new_axisout is not None or \
           op1.new_axisout is not None and op2.axisout is not None:
            #XXX we could handle these cases with a reshape
            return None
        try:
            return merge_none(op1.partitionout, op2.partitionout), \
                   merge_none(op1.partitionin, op2.partitionin)
        except ValueError:
            return None

    @staticmethod
    def _validate_composition(op1, op2):
        axisin1= first_is_not([op1.axisin, op1.new_axisin], None)
        axisout2 = first_is_not([op2.axisout, op2.new_axisout], None)
        if axisin1 < 0 and op2.shapeout is not None:
            axisin1 += len(op2.shapeout)
        if axisout2 < 0 and op1.shapein is not None:
            axisout2 += len(op1.shapein)
        if axisin1 != axisout2:
            return None
        if op1.axisin is not None and op2.new_axisout is not None or \
           op1.new_axisin is not None and op2.axisout is not None:
            #XXX we could handle these cases with a reshape
            return None
        p1 = op1.partitionin
        p2 = op2.partitionout
        if p1 is None or p2 is None:
            return None
        try:
            p = merge_none(p1, p2)
        except ValueError:
            return None
        pout = None if op1.partitionout is None else op1._get_partitionout(
               op1.operands, p, op1.axisin, op1.axisout, op1.new_axisin,
               op1.new_axisout)
        pin = None if op2.partitionin is None else op2._get_partitionin(
               op2.operands, p, op2.axisin, op2.axisout, op2.new_axisin,
               op2.new_axisout)

        return None if pout is None else merge_none(op1.partitionout, pout), \
               None if pin is None else merge_none(op2.partitionin, pin)

    @staticmethod
    def _validate_shapes(shapes, p, axis, new_axis):
        explicit = [s for s in shapes if s is not None]
        if len(explicit) == 0:
            return None
        shape = explicit[0]
        if p is None or new_axis is not None:
            if any([s != shape for s in explicit]):
                raise ValueError("The operands have incompatible shapes: '{0}'"
                                 ".".format(shapes))
            return shape
        rank = len(shape)
        if any([len(s) != rank for s in explicit]):
            raise ValueError("The blocks do not have the same number of dimensi"
                "ons: '{0}'.".format(shapes))
        if any([shapes[i] is not None and shapes[i][axis] != p[i] \
                for i in range(len(p)) if p[i] is not None]):
            raise ValueError("The blocks have shapes '{0}' incompatible with th"
                "e partition {1}.".format(shapes, p))
        if len(explicit) == 1:
            return shape
        ok = [all([s is None or s[i] == shape[i] for s in shapes]) \
              for i in range(rank)]
        ok[axis] = True
        if not all(ok):
            raise ValueError("The dimensions of the blocks '{0}' are not the sa"
                "me along axes other than that of the partition '{1}'.".format(
                shapes, p))
        return shape

    @staticmethod
    def _rule_add_operator(self, op):
        """ Rule for BlockOperator + Operator. """
        if not op.flags.universal:
            return None
        return BlockOperator([o + op for o in self.operands],
            self.partitionin, self.partitionout, self.axisin, self.axisout,
            self.new_axisin, self.new_axisout)

    @staticmethod
    def _rule_right_operator(op, self):
        """ Rule for Operator * BlockOperator. """
        if self.partitionout is None:
            return None
        if isinstance(op, BlockOperator):
            return None
        if not op.flags.universal:
            return None
        n = len(self.partitionout)
        partitionout = self._get_partitionout(n*[op], self.partitionout,
            self.axisout, self.axisout, self.new_axisout, self.new_axisout)
        return BlockOperator([op * o for o in self.operands],
            self.partitionin, partitionout, self.axisin, self.axisout,
            self.new_axisin, self.new_axisout)

    @staticmethod
    def _rule_left_operator(self, op):
        """ Rule for BlockOperator * Operator. """
        if self.partitionin is None:
            return None
        if not op.flags.universal:
            return None
        n = len(self.partitionin)
        partitionin = self._get_partitionin(n*[op], self.partitionin,
            self.axisin, self.axisin, self.new_axisin, self.new_axisin)
        return BlockOperator([o * op for o in self.operands],
            partitionin, self.partitionout, self.axisin, self.axisout,
            self.new_axisin, self.new_axisout)

    @staticmethod
    def _rule_commutative_blockoperator(p1, p2, operation):
        """ Rule for BlockOperator + BlockOperator. """
        partitions = p1._validate_commutative(p1, p2)
        if partitions is None:
            return None
        partitionout, partitionin = partitions
        operands = [operation([o1, o2]) for o1,o2 in \
                    zip(p1.operands, p2.operands)]
        return BlockOperator(operands, partitionin, partitionout,
            p1.axisin, p1.axisout, p1.new_axisin, p1.new_axisout)

    @staticmethod
    def _rule_add_blockoperator(p1, p2):
        return p1._rule_commutative_blockoperator(p1, p2, AdditionOperator)

    @staticmethod
    def _rule_mul_blockoperator(p1, p2):
        return p1._rule_commutative_blockoperator(p1, p2, MultiplicationOperator)

    @staticmethod
    def _rule_comp_blockoperator(p1, p2):
        """ Rule for BlockOperator * BlockOperator. """
        partitions = p1._validate_composition(p1, p2)
        if partitions is None:
            return None
        partitionout, partitionin = partitions
        operands = [o1 * o2 for o1,o2 in zip(p1.operands, p2.operands)]
        if partitionin is partitionout is None:
            return AdditionOperator(operands)
        axisin, axisout = p2.axisin, p1.axisout
        new_axisin, new_axisout = p2.new_axisin, p1.new_axisout
        return BlockOperator(operands, partitionin, partitionout, axisin,
            axisout, new_axisin, new_axisout)


class BlockDiagonalOperator(BlockOperator):
    """
    Block diagonal operator.

    If a new axis 'new_axisin' is specified, the input shapes of the blocks
    must be the same, and the input is iterated along this axis. Otherwise,
    the input shapes of the blocks must be the same except for one same
    dimension 'axisin': the axis along which the input is partitioned.

    If a new axis 'new_axisout' is specified, the output shapes of the blocks
    must be the same, and the output is stacked along this axis. Otherwise,
    the output shapes of the blocks must be the same except for one same
    dimension 'axisout': the axis along which the output is partitioned.
    This operator can be used to process data chunk by chunk.

    This operator can be used to process data chunk by chunk.

    The direct methods of the partition operators may be called with non-C or
    non-Fortran contiguous input and output arrays, so care must be taken when
    interfacing C or Fortran code.

    Parameters
    ----------
    operators : Operator list
        Operators that will populate the diagonal blocks.
    partitionin : tuple of int
        Partition of the number of elements along the input partition axis, to
        be provided if at least one of the input operators is implicit-shape
    axisin : int
        Input partition axis (default is 0)
    axisout : int
        Output partition axis (default is the input partition axis)

    Example
    -------
    o1, o2 = Operator(shapein=(16,4)), Operator(shapein=(16,3))
    p = BlockDiagonalOperator([o1, o2], axisin=-1)
    print(p.shapein)
    (16,7)

    """
    def __init__(self, operands, partitionin=None, axisin=None, axisout=None,
                 new_axisin=None, new_axisout=None):
   
        if axisin is None and new_axisin is None:
            raise NotImplementedError('Free partitioning not implemented yet.')

        if axisout is None:
            axisout = axisin
        if new_axisout is None:
            new_axisout = new_axisin

        if partitionin is None:
            partitionin = self._get_partition([op.shapein \
                for op in self.operands], axisin, new_axisin)
        partitionin = tointtuple(partitionin)
        partitionout = len(partitionin) * (None,)

        BlockOperator.__init__(self, operands, partitionin, partitionout,
                               axisin, axisout, new_axisin, new_axisout)

    def direct(self, input, output):
        if None in self.partitionout:
            partitionout = list(self.partitionout)
            for i, op in enumerate(self.operands):
                if partitionout[i] is not None:
                    continue
                if self.partitionin[i] is None:
                    raise ValueError('The shape of an operator with implicit pa'
                        'rtition cannot be inferred.')
                shapein = list(input.shape)
                shapein[self.axisin] = self.partitionin[i]
                partitionout[i] = op.validatereshapein(shapein)[self.axisout]
        else:
            partitionout = self.partitionout

        memory.up()
        for i, op, sin, sout in zip(range(len(self.operands)), self.operands,
                                    self.get_slicesin(),
                                    self.get_slicesout(partitionout)):
            o = output[sout]
            with memory.push_and_pop(o):
                op.direct(input[sin], o)
        memory.down()


class BlockColumnOperator(BlockOperator):
    """
    Block column operator.

    The input shapes of the blocks must be the same.
    If a new axis 'new_axisout' is specified, the output shapes of the blocks
    must be the same, and the output is stacked along this axis. Otherwise,
    the output shapes of the blocks must be the same except for one same
    dimension 'axisout': the axis along which the output is partitioned.
    This operator can be used to process data chunk by chunk.

    Example
    -------
    >>> I = IdentityOperator(shapein=3)
    >>> op = BlockColumnOperator([I,2*I])
    >>> op.todense()

    array([[ 1.,  0.,  0.],
           [ 0.,  1.,  0.],
           [ 0.,  0.,  1.],
           [ 2.,  0.,  0.],
           [ 0.,  2.,  0.],
           [ 0.,  0.,  2.]])

    """   
    def __init__(self, operands, partitionout=None, axisout=None,
                 new_axisout=None):
        if axisout is None and new_axisout is None:
            raise NotImplementedError('Free partitioning not implemented yet.')

        if partitionout is None:
            partitionout = self._get_partition([op.shapeout \
                for op in self.operands], axisout, new_axisout)
        partitionout = tointtuple(partitionout)

        BlockOperator.__init__(self, operands, partitionout=partitionout,
                               axisout=axisout, new_axisout=new_axisout)
        
    def direct(self, input, output):
        if None in self.partitionout:
            partitionout = list(self.partitionout)
            for i, op in enumerate(self.operands):
                if partitionout[i] is None:
                    partitionout[i] = op.validatereshapein(input.shape)[
                                          self.axisout]
        else:
            partitionout = self.partitionout

        memory.up()
        for op, sout in zip(self.operands, self.get_slicesout(partitionout)):
            o = output[sout]
            with memory.push_and_pop(o):
                op.direct(input, o)
        memory.down()

    def __str__(self):
        operands = ['[{0}]'.format(o) for o in self.operands]
        if len(operands) > 2:
            operands = [operands[0], '...', operands[-1]]
        return '[ ' + ' '.join(operands) + ' ]'


class BlockRowOperator(BlockOperator):
    """
    Block row operator.

    The output shapes of the blocks must be the same.
    If a new axis 'new_axisin' is specified, the input shapes of the blocks
    must be the same, and the input is iterated along this axis. Otherwise,
    the input shapes of the blocks must be the same except for one same
    dimension 'axisin': the axis along which the input is partitioned.
    This operator can be used to process data chunk by chunk.

    Example
    -------
    >>> I = IdentityOperator(shapein=3)
    >>> op = BlockRowOperator([I,2*I])
    >>> op.todense()

    array([[ 1.,  0.,  0., 2., 0., 0.],
           [ 0.,  1.,  0., 0., 2., 0.],
           [ 0.,  0.,  1., 0., 0., 2.]])

    """   
    def __init__(self, operands, partitionin=None, axisin=None,
                 new_axisin=None):
        if axisin is None and new_axisin is None:
            raise NotImplementedError('Free partitioning not implemented yet.')

        if partitionin is None:
            partitionin = self._get_partition([op.shapein \
                for op in self.operands], axisin, new_axisin)
        partitionin = tointtuple(partitionin)

        BlockOperator.__init__(self, operands, partitionin=partitionin,
                               axisin=axisin, new_axisin=new_axisin)

    def direct(self, input, output):
        if None in self.partitionin:
            partitionin = list(self.partitionin)
            for i, op in enumerate(self.operands):
                if partitionin[i] is None:
                    partitionin[i] = op.validatereshapeout(output.shape)[
                                         self.axisin]
        else:
            partitionin = self.partitionin

        #XXX optimize me
        work = np.zeros_like(output)
        for op, sin in zip(self.operands, self.get_slicesin(partitionin)):
            op.direct(input[sin], output)
            work += output
        output[...] = work

    def __str__(self):
        operands = [str(o) for o in self.operands]
        if len(operands) > 2:
            operands = [operands[0], '...', operands[-1]]
        return '[[ ' + ' '.join(operands) + ' ]]'


@linear
@real
@inplace
class ReshapeOperator(Operator):
    """
    Operator that reshapes arrays.

    Example
    -------
    >>> op = ReshapeOperator(6, (3,2))
    >>> op(np.ones(6)).shape
    (3, 2)
    """
    def __init__(self, shapein, shapeout, **keywords):
        if shapein is None:
            raise ValueError('The input shape is None.')
        if shapeout is None:
            raise ValueError('The output shape is None.')
        shapein = tointtuple(shapein)
        shapeout = tointtuple(shapeout)
        if np.product(shapein) != np.product(shapeout):
            raise ValueError('The total size of the output must be unchanged.')
        if shapein == shapeout:
            self.__class__ = IdentityOperator
            self.__init__(shapein, **keywords)
            return
        Operator.__init__(self, shapein=shapein, shapeout=shapeout, **keywords)
        self.set_rule('.T', lambda s: ReverseOperatorFactory(ReshapeOperator,s))
        self.set_rule('.T.', '1', CompositionOperator)

    def direct(self, input, output):
        if self.same_data(input, output):
            pass
        output.ravel()[:] = input.ravel()

    def __str__(self):
        return strshape(self.shapeout) + '←' + strshape(self.shapein)


class BroadcastingOperator(Operator):
    """
    Abstract class for operators that operate on a data array and
    the input array, and for which broadcasting of the data array across
    the input array is required.

    Leftward broadcasting is the normal numpy's broadcasting along the slow
    dimensions, if the array is stored in C order. Rightward broadcasting is
    a broadcasting along the fast dimension.
    """
    def __init__(self, data, broadcast='disabled', shapeout=None, dtype=None,
                 **keywords):
        if data is None:
            raise ValueError('The input data is None.')
        data = np.asarray(data)
        if dtype is None:
            dtype = data.dtype
        data = np.array(data, dtype, order='c', copy=False)
        if data.ndim == 0:
            broadcast = 'scalar'
        broadcast = broadcast.lower()
        values = ('leftward', 'rightward', 'disabled', 'scalar')
        if broadcast not in values:
            raise ValueError("Invalid value '{0}' for the broadcast keyword. Ex"
                "pected values are {1}.".format(broadcast, strenum(values)))
        if broadcast == 'disabled':
            if shapeout not in (None, data.shape):
                raise ValueError("The input shapein is incompatible with the da"
                                 "ta shape.")
            shapeout = data.shape
        self.broadcast = broadcast
        self.data = data
        Operator.__init__(self, shapeout=shapeout, dtype=dtype, **keywords)
        self.set_rule('{BroadcastingOperator}.', lambda b1, b2: \
            self._rule_broadcast(b1, b2, np.add), AdditionOperator)
        self.set_rule('{BroadcastingOperator}.', lambda b1, b2: \
            self._rule_broadcast(b1, b2, np.multiply), CompositionOperator)

    @staticmethod
    def _rule_broadcast(b1, b2, operation):
        # check the direct subclasses of Broadcasting for each operand
        i1 = b1.__class__.__mro__.index(BroadcastingOperator) - 1
        try:
            i2 = b2.__class__.__mro__.index(BroadcastingOperator) - 1
        except ValueError:
            i2 = -1
        if i1 == i2 == -1:
            cls = BroadcastingOperator
        elif i1 == -1:
            cls = b2.__class__.__mro__[i2]
        elif i2 == -1:
            cls = b1.__class__.__mro__[i1]
        else:
            cls = b1.__class__.__mro__[i1]
            if cls is not b2.__class__.__mro__[i2]:
                return None

        # check broadcast
        b = set([b1.broadcast, b2.broadcast])
        if 'leftward' in b and 'rightward' in b:
            return None
        if 'disabled' in b:
            broadcast = 'disabled'
        elif 'leftward' in b:
            broadcast = 'leftward'
        elif 'rightward' in b:
            broadcast = 'rightward'
        else:
            broadcast = 'scalar'
        if 'rightward' in b:
            data = operation(b1.data.T, b2.data.T).T
        else:
            data = operation(b1.data, b2.data)
        
        return cls(data, broadcast)

    def _as_strided(self, shape):
        strides = len(shape) * [0]
        if self.broadcast == 'rightward':
            delta = 0
        else:
            delta = len(shape) - self.data.ndim
        v = self.data.itemsize
        for i in range(self.data.ndim-1, -1, -1):
            s = self.data.shape[i]
            if s == 1:
                continue
            strides[i+delta] = v
            v *= s
        return np.lib.stride_tricks.as_strided(self.data, shape, strides)


@symmetric
@inplace
class DiagonalOperator(BroadcastingOperator):
    """
    Diagonal operator.

    Arguments
    ---------

    data : ndarray
      The diagonal coefficients

    broadcast : 'rightward' or 'disabled' (default 'disabled')
      If broadcast == 'rightward', the diagonal is broadcasted along the fast
      axis.

    Exemple
    -------
    >>> A = DiagonalOperator(arange(1, 6, 2))
    >>> A.todense()

    array([[1, 0, 0],
           [0, 3, 0],
           [0, 0, 5]])

    >>> A = DiagonalOperator(arange(1, 3), broadcast='rightward', shapein=(2, 2))
    >>> A.todense()

    array([[1, 0, 0, 0],
           [0, 1, 0, 0],
           [0, 0, 2, 0],
           [0, 0, 0, 2]])
    """
    def __init__(self, data, broadcast='disabled', **keywords):
        data = np.asarray(data)
        if not isinstance(self, HomothetyOperator) and \
           np.all(data == data.flat[0]):
            if broadcast == 'disabled' and data.ndim > 0:
                keywords['shapein'] = data.shape
            self.__class__ = HomothetyOperator
            self.__init__(data.flat[0], **keywords)
            return
        BroadcastingOperator.__init__(self, data, broadcast, **keywords)
        self.set_rule('.{BlockOperator}', lambda s,o: self._rule_left_block(s,
                      o, CompositionOperator), CompositionOperator)
        self.set_rule('{BlockOperator}.', lambda o,s: self._rule_right_block(o,
                      s, CompositionOperator), CompositionOperator)
        self.set_rule('.{BlockOperator}', lambda s,o: self._rule_left_block(s,
                      o, AdditionOperator), AdditionOperator)
        self.set_rule('.{BlockOperator}', lambda s,o: self._rule_left_block(s,
                      o, MultiplicationOperator), MultiplicationOperator)

    def direct(self, input, output):
        if self.broadcast == 'rightward':
            np.multiply(input.T, self.data.T, output.T)
        else:
            np.multiply(input, self.data, output)

    def conjugate_(self, input, output):
        if self.broadcast == 'rightward':
            np.multiply(input.T, np.conjugate(self.data).T, output.T)
        else:
            np.multiply(input, np.conjugate(self.data), output)

    def inverse(self, input, output):
        if self.broadcast == 'rightward':
            np.divide(input.T, self.data.T, output.T)
        else:
            np.divide(input, self.data, output)

    def inverse_conjugate(self, input, output):
        if self.broadcast == 'rightward':
            np.divide(input.T, np.conjugate(self.data).T, output.T)
        else:
            np.divide(input, np.conjugate(self.data), output)

    def validatein(self, shape):
        if self.data.size == 1:
            return
        n = self.data.ndim
        if len(shape) < n:
            raise ValueError("Invalid number of dimensions.")
        
        if self.broadcast == 'rightward':
            it = zip(shape[:n], self.data.shape[:n])
        else:
            it = zip(shape[-n:], self.data.shape[-n:])
        for si, sd in it:
            if sd != 1 and sd != si:
                raise ValueError("The data array cannot be broadcast across the"
                                 " input.")

    def toshapein(self, v):
        if self.shapein is not None:
            return v.reshape(self.shapein)
        if self.data.ndim < 1:
            return v

        sd = list(self.data.shape)
        n = sd.count(1)
        if n > 1:
            raise ValueError('Ambiguous broadcasting.')
        if n == 0:
            if self.broadcast == 'rightward':
                sd.append(-1)
            else:
                sd.insert(0, -1)
        else:
            sd[sd.index(1)] = -1
        
        try:
            v = v.reshape(sd)
        except ValueError:
            raise ValueError("Invalid broadcasting.")

        return v

    @staticmethod
    def _rule_block(self, op, shape, partition, axis, new_axis, func):
        if partition is None:
            return
        if None in partition and self.broadcast != 'scalar':
            return

        b = self.broadcast
        ndim = self.data.ndim
        axis_ = first_is_not([axis, new_axis], None)
            
        do_replicate = False
        if b == 'scalar':
            do_replicate = True
        elif b == 'disabled':
            pass
        elif shape is None:
            if new_axis is not None and ndim == 1 and (new_axis == -1 and 
               b == 'rightward' or new_axis ==  0 and b == 'leftward'):
                do_replicate = True
            elif b == 'rightward':
                if axis_ > ndim:
                    do_replicate = True
                elif axis_ < 0:
                    return
            else:
                if axis_ < -ndim:
                    do_replicate = True
                elif axis_ >= 0:
                    return
        else:
            if b == 'rightward':
                if axis_ >= ndim:
                    do_replicate = True
            else:
                if axis is not None:
                    axis = axis - len(shape)
                else:
                    new_axis = new_axis - len(shape)
                if axis_ - len(shape) < -ndim:
                    do_replicate = True
        if do_replicate:
            ops = [func(self, o) for o in op.operands]
        else:
            slices = op._get_slices(partition, axis, new_axis)
            ops = [func(DiagonalOperator(self.data[s], broadcast=
                   self.broadcast), o) for s, o in zip(slices, op.operands)]

        return BlockOperator(ops, op.partitionin, op.partitionout, op.axisin,
                             op.axisout, op.new_axisin, op.new_axisout)

    @staticmethod
    def _rule_left_block(self, op, cls):
        func = lambda d, b: cls([d, b])
        return DiagonalOperator._rule_block(self, op, op.shapeout,
            op.partitionout, op.axisout, op.new_axisout, func)

    @staticmethod
    def _rule_right_block(op, self, cls):
        func = lambda d, b: cls([b, d])
        return DiagonalOperator._rule_block(self, op, op.shapein,
            op.partitionin, op.axisin, op.new_axisin, func)


@inplace
@universal
class HomothetyOperator(DiagonalOperator):
    """
    Multiplication by a scalar.

    """
    def __init__(self, data, **keywords):
        data = np.asarray(data)
        if data.ndim > 0:
            raise ValueError("Invalid data size '{0}' for HomothetyOperator." \
                             .format(data.size))
        if not isinstance(self, ZeroOperator) and data == 0:
            self.__class__ = ZeroOperator
            self.__init__(**keywords)
            return
        if not isinstance(self, IdentityOperator) and data == 1:
            self.__class__ = IdentityOperator
            self.__init__(**keywords)
            return
        if data == -1:
            keywords['flags'] = {'involutary':True}

        DiagonalOperator.__init__(self, data, 'scalar', **keywords)
        self.set_rule('.C', lambda s: DirectOperatorFactory(HomothetyOperator,
                      s, np.conjugate(s.data)))
        self.set_rule('.I', lambda s: DirectOperatorFactory(HomothetyOperator,
                      s, 1/s.data if s.data != 0 else np.nan))
        self.set_rule('.IC', lambda s: DirectOperatorFactory(HomothetyOperator,
                      s, np.conjugate(1/s.data) if s.data != 0 else np.nan))
        self.set_rule('{Operator}.', self._rule_right, CompositionOperator)

    def __str__(self):
        data = self.data.flat[0]
        if data == int(data):
            data = int(data)
        if data == 1:
            return 'I'
        if data == -1:
            return '-I'
        return str(data) + 'I'

    @staticmethod
    def _rule_right(operator, self):
        if operator.flags.linear:
            return self, operator


@real
@idempotent
@involutary
@inplace
class IdentityOperator(HomothetyOperator):
    """
    A subclass of HomothetyOperator with data = 1.

    Exemple
    -------
    >>> I = IdentityOperator()
    >>> I.todense(3)

    array([[ 1.,  0.,  0.],
           [ 0.,  1.,  0.],
           [ 0.,  0.,  1.]])

    >>> I = IdentityOperator(shapein=2)
    >>> I * arange(2)
    ndarraywrap([ 0.,  1.])

    """
    def __init__(self, shapein=None, **keywords):
        HomothetyOperator.__init__(self, 1, shapein=shapein, **keywords)
        self.set_rule('.{Operator}', self._rule_left, CompositionOperator)

    def direct(self, input, output):
        if self.same_data(input, output):
            pass
        output[...] = input

    @staticmethod
    def _rule_left(self, operator):
        return operator.copy()

    @staticmethod
    def _rule_right(operator, self):
        return operator.copy()


@idempotent
@inplace
class ConstantOperator(BroadcastingOperator):
    """
    Non-linear constant operator.
    """
    def __init__(self, data, broadcast='disabled', **keywords):
        data = np.asarray(data)
        if data.ndim > 0 and np.all(data == data.flat[0]):
            if broadcast == 'disabled':
                keywords['shapeout'] = data.shape
            self.__init__(data.flat[0], 'scalar', **keywords)
            return
        if not isinstance(self, ZeroOperator) and data.ndim == 0  and data == 0:
            self.__class__ = ZeroOperator
            self.__init__(**keywords)
            return
        BroadcastingOperator.__init__(self, data, broadcast, **keywords)
        self.set_rule('.C', lambda s: DirectOperatorFactory(ConstantOperator, s,
                      s.data.conjugate(), broadcast=s.broadcast))
        if self.flags.shape_input == 'unconstrained' and \
           self.flags.shape_output != 'implicit':
            self.set_rule('.T', '.')
        self.set_rule('.{Operator}', self._rule_left, CompositionOperator)
        self.set_rule('{Operator}.', self._rule_right, CompositionOperator)
        self.set_rule('.{CompositionOperator}', self._rule_mul,
                      MultiplicationOperator)
        self.set_rule('.{DiagonalOperator}', self._rule_mul,
                      MultiplicationOperator)

    def direct(self, input, output, operation=operation_assignment):
        if self.broadcast == 'rightward':
            operation(output.T, self.data.T)
        else:
            operation(output, self.data)

    @staticmethod
    def _rule_left(self, op):
        return self.copy()

    @staticmethod
    def _rule_right(op, self):
        if op.flags.shape_output == 'unconstrained':
            return None
        if self.flags.shape_output == 'explicit':
            data = self._as_strided(self.shapeout)
        elif op.flags.shape_input == 'explicit':
            data = self._as_strided(op.shapein)
        else:
            return None
        return ConstantOperator(op(data))

    @staticmethod
    def _rule_mul(self, op):
        return CompositionOperator([DiagonalOperator(self.data, broadcast= \
            self.broadcast, shapein=self.shapeout), op])

    def __str__(self):
        return str(self.data)

    def __neg__(self):
        return ConstantOperator(-self.data, broadcast=self.broadcast,
            shapein=self.shapein, shapeout=self.shapeout,
            reshapein=self.reshapein, reshapeout=self.reshapeout,
            dtype=self.dtype)


@linear
@real
class ZeroOperator(ConstantOperator):
    """
    A subclass of ConstantOperator with data = 0.
    """
    def __init__(self, **keywords):
        ConstantOperator.__init__(self, 0, **keywords)
        self.set_rule('.T', lambda s: ReverseOperatorFactory(ZeroOperator, s))

    def direct(self, input, output, operation=operation_assignment):
        operation(output, 0)

    @staticmethod
    def _rule_right(op, self):
        if op.flags.linear:
            return self.copy()
        return super(ZeroOperator, self)._rule_right(op, self)

    def __neg__(self):
        return self

I = IdentityOperator()
O = ZeroOperator()
