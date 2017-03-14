from __future__ import print_function, division, absolute_import
from numba import ir
from numba.ir_utils import *
#from numba.annotations import type_annotations
from numba import types, config
from numba.typing import npydecl

import numpy
MAP_TYPES = [numpy.ufunc]

class ArrayAnalysis(object):
    """Analyzes Numpy array computations for properties such as shapes
    and equivalence classes.
    """

    def __init__(self, func_ir, typemap, calltypes):
        self.func_ir = func_ir
        self.typemap = typemap
        self.calltypes = calltypes
        self.next_eq_class = 1
        # equivalence classes for each dimension of each array are saved
        # string to tuple of class numbers
        # example: {'A':[1,2]}
        #          {1:[n,a],2:[k,m,3]}
        self.array_shape_classes = {}
        # class zero especial and is size 1 for constants
        # and added broadcast dimensions
        # -1 class means unknown
        self.class_sizes = {0:[1]}
        # size variable to use for each array dimension
        self.array_size_vars = {}
        # keep a list of numpy Global variables to find numpy calls
        self.numpy_globals = []
        # calls that are essentially maps like DUFunc
        self.map_calls = []
        # keep numpy call variables with their call names
        self.numpy_calls = {}
        # keep attr calls to arrays like t=A.sum() as {t:('sum',A)}
        self.array_attr_calls = {}
        # keep tuple builds like {'t':[a,b],}
        self.tuple_table = {}

    def run(self):
        # TODO: ignoring CFG for now
        if config.DEBUG_ARRAY_OPT==1:
            print("starting array analysis")
            self.func_ir.dump()
        for (key, block) in self.func_ir.blocks.items():
            self._analyze_block(block)
        if config.DEBUG_ARRAY_OPT==1:
            print("classes: ", self.array_shape_classes)
            print("class sizes: ", self.class_sizes)
            print("numpy globals ", self.numpy_globals)
            print("numpy calls ", self.numpy_calls)
            print("array attr calls ", self.array_attr_calls)
            print("tuple table ", self.tuple_table)

    def _analyze_block(self, block):
        out_body = []
        for inst in block.body:
            # instructions can generate extra size calls to be appended.
            # if an array doesn't have a size variable for a dimension,
            # a size variable should be generated when the array is created
            generated_size_calls = self._analyze_inst(inst)
            out_body.append(inst)
            for node in generated_size_calls:
                out_body.append(node)
        block.body = out_body

    def _analyze_inst(self, inst):
        if isinstance(inst, ir.Assign):
            return self._analyze_assign(inst)
        return []

    def _analyze_assign(self, assign):
        lhs = assign.target.name
        rhs = assign.value
        if isinstance(rhs, ir.Global):
            for T in MAP_TYPES:
                if isinstance(rhs.value, T):
                    self.map_calls.append(lhs)
            if rhs.value.__name__=='numpy':
                self.numpy_globals.append(lhs)
        if isinstance(rhs, ir.Expr) and rhs.op=='getattr':
            if rhs.value.name in self.numpy_globals:
                self.numpy_calls[lhs] = rhs.attr
            elif self._isarray(rhs.value.name):
                self.array_attr_calls[lhs] = (rhs.attr, rhs.value.name)
        if isinstance(rhs, ir.Expr) and rhs.op=='build_tuple':
            self.tuple_table[lhs] = rhs.items
        if isinstance(rhs, ir.Const) and isinstance(rhs.value, tuple):
            self.tuple_table[lhs] = rhs.value

        #rhs_class_out = self._analyze_rhs_classes(rhs)
        size_calls = []
        if self._isarray(lhs):
            rhs_corr = self._analyze_rhs_classes(rhs).copy()
            if lhs in self.array_shape_classes:
                # if shape already inferred in another basic block,
                # make sure this new inference is compatible
                if self.array_shape_classes[lhs]!=rhs_corr:
                    self.array_shape_classes[lhs] = [-1]*self._get_ndims(lhs)
                    self.array_size_vars.pop(lhs, None)
                    print("incompatible array shapes in control flow")
                    return []
            self.array_shape_classes[lhs] = rhs_corr
            self.array_size_vars[lhs] = [-1]*self._get_ndims(lhs)
            # make sure output lhs array has size variables for each dimension
            for (i,corr) in enumerate(rhs_corr):
                # if corr unknown or new
                if corr==-1 or corr not in self.class_sizes.keys():
                    # generate size call nodes for this dimension
                    nodes = self._gen_size_call(assign.target, i)
                    size_calls += nodes
                    assert isinstance(nodes[-1], ir.Assign)
                    size_var = nodes[-1].target
                    if corr!=-1:
                        self.class_sizes[corr] = [size_var]
                    self.array_size_vars[lhs][i] = size_var
                else:
                    # reuse a size variable from this correlation
                    # TODO: consider CFG?
                    self.array_size_vars[lhs][i] = self.class_sizes[corr][0]

        #print(self.array_shape_classes)
        return size_calls

    def _gen_size_call(self, var, i):
        out = []
        ndims = self._get_ndims(var.name)
        # attr call: A_sh_attr = getattr(A, shape)
        shape_attr_call = ir.Expr.getattr(var, "shape", var.loc)
        attr_var = ir.Var(var.scope, mk_unique_var(var.name+"_sh_attr"+str(i)), var.loc)
        self.typemap[attr_var.name] = types.containers.UniTuple(types.int64, ndims)
        attr_assign = ir.Assign(shape_attr_call, attr_var, var.loc)
        out.append(attr_assign)
        # const var for dim: $constA0 = Const(0)
        const_node = ir.Const(i, var.loc)
        const_var = ir.Var(var.scope, mk_unique_var("$const"+var.name+str(i)), var.loc)
        self.typemap[const_var.name] = types.int64
        const_assign = ir.Assign(const_node, const_var, var.loc)
        out.append(const_assign)
        # get size: Asize0 = A_sh_attr[0]
        size_var = ir.Var(var.scope, mk_unique_var(var.name+"size"+str(i)), var.loc)
        self.typemap[size_var.name] = types.int64
        getitem_node = ir.Expr.static_getitem(attr_var, i, const_var, var.loc)
        self.calltypes[getitem_node] = None
        getitem_assign = ir.Assign(getitem_node, size_var, var.loc)
        out.append(getitem_assign)
        return out

    # lhs is array so rhs has to return array
    def _analyze_rhs_classes(self, node):
        if isinstance(node, ir.Arg):
            assert self._isarray(node.name)
            return self._add_array_corr(node.name)
        elif isinstance(node, ir.Var):
            return self.array_shape_classes[node.name].copy()
        elif isinstance(node, ir.Expr):
            if node.op=='unary' and node.fn in UNARY_MAP_OP:
                assert isinstance(node.value, ir.Var)
                in_var = node.value.name
                assert self._isarray(in_var)
                return self.array_shape_classes[in_var].copy()
            elif node.op=='binop' and node.fn in BINARY_MAP_OP:
                arg1 = node.lhs.name
                arg2 = node.rhs.name
                return self._broadcast_and_match_shapes([arg1, arg2])
            elif node.op=='inplace_binop' and node.immutable_fn in BINARY_MAP_OP:
                arg1 = node.lhs.name
                arg2 = node.rhs.name
                return self._broadcast_and_match_shapes([arg1, arg2])
            elif node.op=='arrayexpr':
                # set to remove duplicates
                args = {v.name for v in node.list_vars()}
                return self._broadcast_and_match_shapes(list(args))
            elif node.op=='cast':
                return self.array_shape_classes[node.value.name].copy()
            elif node.op=='call':
                call_name = 'NULL'
                args = node.args.copy()
                if node.func.name in self.map_calls:
                    return self.array_shape_classes[args[0].name].copy()
                if node.func.name in self.numpy_calls.keys():
                    call_name = self.numpy_calls[node.func.name]
                elif node.func.name in self.array_attr_calls.keys():
                    call_name, arr = self.array_attr_calls[node.func.name]
                    args.insert(0,arr)
                assert call_name is not 'NULL'
                return self._analyze_np_call(call_name, args)
            elif node.op=='getattr' and self._isarray(node.value.name):
                # matrix transpose
                if node.attr=='T':
                    return self._analyze_np_call('transpose', [node.value])
            else:
                print("can't find shape classes for expr",node," of op",node.op)
        print("can't find shape classes for node",node," of type ",type(node))
        return []

    def _analyze_np_call(self, call_name, args):
        #print("numpy call ",call_name,args)
        if call_name=='transpose':
            out_eqs = self.array_shape_classes[args[0].name].copy()
            out_eqs.reverse()
            return out_eqs
        elif call_name in ['empty', 'zeros', 'ones']:
            return self._get_classes_from_shape(args[0].name)
        elif call_name in ['empty_like', 'zeros_like', 'ones_like']:
            # shape same as input
            return self.array_shape_classes[args[0].name].copy()
        elif call_name=='reshape':
            # TODO: infer shape from length of args[0] in case of -1 input
            # shape is either Int or tuple of Int
            return self._get_classes_from_shape(args[1].name)
        elif call_name=='dot':
            # https://docs.scipy.org/doc/numpy/reference/generated/numpy.dot.html
            # for multi-dimensional arrays, last dimension of arg1 and second
            # to last dimension of arg2 should be equal since used in dot product.
            # if arg2 is 1D, its only dimension is used for dot product and
            # should be equal to second to last of arg1.
            assert len(args)==2 or len(args)==3
            in1 = args[0].name
            in2 = args[1].name
            ndims1 = self._get_ndims(in1)
            ndims2 = self._get_ndims(in2)
            c1 = self.array_shape_classes[in1][ndims1-1]
            c2 = 0

            if ndims2==1:
                c2 = self.array_shape_classes[in2][0]
            else:
                c2 = self.array_shape_classes[in2][ndims2-2]

            c_inner = self._merge_classes(c1,c2)

            c_out = []
            for i in range(ndims1-1):
                c_out.append(self.array_shape_classes[in1][i])
            for i in range(ndims2-2):
                c_out.append(self.array_shape_classes[in2][i])
            if ndims2>1:
                c_out.append(self.array_shape_classes[in2][ndims2-1])
            #print("dot class ",c_out)
            return c_out
        elif call_name in UFUNC_MAP_OP:
            return self._broadcast_and_match_shapes([a.name for a in args])

        print("unknown numpy call:", call_name)
        return [-1]

    def _get_classes_from_shape(self, shape_arg):
        # shape is either Int or tuple of Int
        arg_typ = self.typemap[shape_arg]
        if isinstance(arg_typ, types.scalars.Integer):
            new_class = self._get_next_class()
            self.class_sizes[new_class] = [args[0]]
            return [new_class]
        assert isinstance(arg_typ, types.containers.UniTuple)
        out_eqs = []
        for i in range(arg_typ.count):
            new_class = self._get_next_class()
            out_eqs.append(new_class)
            self.class_sizes[new_class] = [self.tuple_table[shape_arg][i]]
        return out_eqs

    def _merge_classes(self, c1, c2):
        # no need to merge if equal classes already
        if c1==c2:
            return c1

        new_class = self._get_next_class()
        for l in self.array_shape_classes.values():
            for i in range(len(l)):
                if l[i]==c1 or l[i]==c2:
                    l[i] = new_class
        # merge lists of size vars and remove previous classes
        self.class_sizes[new_class] = (self.class_sizes.pop(c1, [])
            + self.class_sizes.pop(c2, []))
        return new_class

    def _broadcast_and_match_shapes(self, args):
        """Infer shape equivalence of arguments based on Numpy broadcast rules
        and return shape of output
        https://docs.scipy.org/doc/numpy/user/basics.broadcasting.html
        """
        # at least one input has to be array, rest are constants
        assert any([self._isarray(a) for a in args])
        # list of array equivalences
        eqs = []
        for a in args:
            if self._isarray(a):
                eqs.append(self.array_shape_classes[a].copy())
            else:
                eqs.append = [0] # constant variable
        ndims = max([len(e) for e in eqs])
        for e in eqs:
            # prepend zeros to match shapes (broadcast rules)
            while len(e)<ndims:
                e.insert(0,0)
        out_eq = [-1 for i in range(ndims)]

        for i in range(ndims):
            c = eqs[0][i]
            for e in eqs:
                if e[i]!=0 and e[i]!=c:
                    if c==0:
                        c = e[i]
                    else:
                        c = self._merge_classes(c, e[i])
            out_eq[i] = c

        return out_eq

    def _isarray(self, varname):
        return isinstance(self.typemap[varname],
                          types.npytypes.Array)

    def _add_array_corr(self, varname):
        assert varname not in self.array_shape_classes
        self.array_shape_classes[varname] = []
        arr_typ = self.typemap[varname]
        for i in range(arr_typ.ndim):
            new_class = self._get_next_class()
            self.array_shape_classes[varname].append(new_class)
        return self.array_shape_classes[varname]

    def _get_next_class(self):
        m = self.next_eq_class
        self.next_eq_class += 1
        return m

    def _get_ndims(self, arr):
        return len(self.array_shape_classes[arr])

UNARY_MAP_OP = npydecl.NumpyRulesUnaryArrayOperator._op_map.keys()
BINARY_MAP_OP = npydecl.NumpyRulesArrayOperator._op_map.keys()
UFUNC_MAP_OP = [f.__name__ for f in npydecl.supported_ufuncs]
