import backend
import ufl
from pyadjoint.tape import get_working_tape
from pyadjoint.block import Block
from .types import Function, DirichletBC
from .types.function_space import extract_subfunction

# Type dependencies
import dolfin

# TODO: Clean up: some inaccurate comments. Reused code. Confusing naming with dFdm when denoting the control as c.


def solve(*args, **kwargs):
    annotate_tape = kwargs.pop("annotate_tape", True)

    if annotate_tape:
        tape = get_working_tape()
        block = SolveBlock(*args, **kwargs)
        tape.add_block(block)

    output = backend.solve(*args, **kwargs)

    if annotate_tape:
        # TODO: Consider if this should be here or in the block constructor.
        #       The immediate reason output isn't added in the block constructor is because it should happen after
        #       the backend call, but the block must be constructed (add dependencies) before the backend call.
        if hasattr(args[1], "create_block_output"):
            block_output = args[1].create_block_output()
        else:
            block_output = args[1].function.create_block_output()
        block.add_output(block_output)

    return output


class SolveBlock(Block):
    def __init__(self, *args, **kwargs):
        super(SolveBlock, self).__init__()
        if isinstance(args[0], ufl.equation.Equation):
            # Variational problem.
            eq = args[0]
            self.lhs = eq.lhs
            self.rhs = eq.rhs
            self.func = args[1]

            # Store boundary conditions in a list.
            if len(args) > 2:
                if isinstance(args[2], list):
                    self.bcs = args[2]
                else:
                    self.bcs = [args[2]]
            else:
                self.bcs = []
            #self.add_output(self.func.create_block_output())
        else:
            # Linear algebra problem.
            # TODO: Consider checking if attributes exist.
            A = args[0]
            u = args[1]
            b = args[2]

            self.lhs = A.form
            self.rhs = b.form
            self.bcs = A.bcs
            self.func = u.function

        if isinstance(self.lhs, ufl.Form) and isinstance(self.rhs, ufl.Form):
            self.linear = True
            # Add dependence on coefficients on the right hand side.
            for c in self.rhs.coefficients():
                self.add_dependency(c.get_block_output())
        else:
            self.linear = False

        for bc in self.bcs:
            self.add_dependency(bc.get_block_output())

        for c in self.lhs.coefficients():
            self.add_dependency(c.get_block_output())

    def __str__(self):
        return "{} = {}".format(str(self.lhs), str(self.rhs))

    def evaluate_adj(self):
        #t = backend.Timer("Solve:evaluate_adj")
        #t4 = backend.Timer("Solve:adj:Prolog")
        fwd_block_output = self.get_outputs()[0]
        u = fwd_block_output.get_output()
        V = u.function_space()
        adj_var = Function(V)

        if self.linear:
            tmp_u = Function(self.func.function_space()) # Replace later? Maybe save function space on initialization.
            F_form = backend.action(self.lhs, tmp_u) - self.rhs
        else:
            tmp_u = self.func
            F_form = self.lhs

        replaced_coeffs = {}
        for block_output in self.get_dependencies():
            coeff = block_output.get_output()
            if coeff in F_form.coefficients():
                replaced_coeffs[coeff] = block_output.get_saved_output()

        replaced_coeffs[tmp_u] = fwd_block_output.get_saved_output()

        F_form = backend.replace(F_form, replaced_coeffs)

        dFdu = backend.derivative(F_form, fwd_block_output.get_saved_output(), backend.TrialFunction(u.function_space()))
        dFdu = backend.assemble(dFdu)

        # Get dJdu from previous calculations.
        dJdu = fwd_block_output.get_adj_output()

        # TODO: It might make sense to move this so we don't have to do the computations above.
        if dJdu is None:
            return

        # Homogenize and apply boundary conditions on adj_dFdu and dJdu.
        bcs = []
        for bc in self.bcs:
            if isinstance(bc, backend.DirichletBC):
                bc = backend.DirichletBC(bc)
                bc.homogenize()
            bcs.append(bc)
            bc.apply(dFdu)

        dFdu_mat = backend.as_backend_type(dFdu).mat()
        dFdu_mat.transpose(dFdu_mat)

        backend.solve(dFdu, adj_var.vector(), dJdu)

        for block_output in self.get_dependencies():
            c = block_output.get_output()
            if c != self.func or self.linear:
                c_rep = replaced_coeffs.get(c, c)

                if isinstance(c, backend.Function):
                    tmp_adj_var = adj_var.copy(deepcopy=True)
                    for bc in bcs:
                        bc.apply(tmp_adj_var.vector())
                    dFdm = -backend.derivative(F_form, c_rep, backend.TrialFunction(c.function_space()))
                    dFdm = backend.adjoint(dFdm)
                    dFdm = dFdm*tmp_adj_var
                    dFdm = backend.assemble(dFdm)

                    block_output.add_adj_output(dFdm)
                elif isinstance(c, backend.Constant):
                    dFdm = -backend.derivative(F_form, c_rep, backend.Constant(1))
                    dFdm = backend.assemble(dFdm)

                    [bc.apply(dFdm) for bc in bcs]

                    block_output.add_adj_output(dFdm.inner(adj_var.vector()))
                elif isinstance(c, backend.DirichletBC):
                    tmp_bc = backend.DirichletBC(c.function_space(), extract_subfunction(adj_var, c.function_space()), *c.domain_args)

                    block_output.add_adj_output([tmp_bc])
                elif isinstance(c, backend.Expression):
                    dFdm = -backend.derivative(F_form, c_rep, backend.TrialFunction(V)) # TODO: What space to use?
                    dFdm = backend.assemble(dFdm)

                    dFdm_mat = backend.as_backend_type(dFdm).mat()

                    import numpy as np
                    bc_rows = []
                    for bc in bcs:
                        for key in bc.get_boundary_values():
                            bc_rows.append(key)

                    dFdm.zero(np.array(bc_rows, dtype=np.intc))

                    dFdm_mat.transpose(dFdm_mat)

                    block_output.add_adj_output([[dFdm*adj_var.vector(), V]])

    def evaluate_tlm(self):
        fwd_block_output = self.get_outputs()[0]
        u = fwd_block_output.get_output()
        V = u.function_space()

        if self.linear:
            tmp_u = Function(self.func.function_space()) # Replace later? Maybe save function space on initialization.
            F_form = backend.action(self.lhs, tmp_u) - self.rhs
        else:
            tmp_u = self.func
            F_form = self.lhs

        replaced_coeffs = {}
        for block_output in self.get_dependencies():
            coeff = block_output.get_output()
            if coeff in F_form.coefficients():
                replaced_coeffs[coeff] = block_output.get_saved_output()

        replaced_coeffs[tmp_u] = fwd_block_output.get_saved_output()

        F_form = backend.replace(F_form, replaced_coeffs)

        # Obtain dFdu.
        dFdu = backend.derivative(F_form, fwd_block_output.get_saved_output(), backend.TrialFunction(u.function_space()))

        dFdu = backend.assemble(dFdu)

        # Homogenize and apply boundary conditions on dFdu.
        bcs = []
        for bc in self.bcs:
            if isinstance(bc, backend.DirichletBC):
                bc = backend.DirichletBC(bc)
                bc.homogenize()
            bcs.append(bc)
            bc.apply(dFdu)

        for block_output in self.get_dependencies():
            tlm_value = block_output.tlm_value
            if tlm_value is None:
                continue

            c = block_output.get_output()
            c_rep = replaced_coeffs.get(c, c)

            if c == self.func:
                continue

            if isinstance(c, backend.Function):
                #dFdm = -backend.derivative(F_form, c_rep, backend.Function(V, tlm_value))
                dFdm = -backend.derivative(F_form, c_rep, tlm_value)
                dFdm = backend.assemble(dFdm)

                # Zero out boundary values from boundary conditions as they do not depend (directly) on c.
                for bc in bcs:
                    bc.apply(dFdm)

            elif isinstance(c, backend.Constant):
                dFdm = -backend.derivative(F_form, c_rep, tlm_value)
                dFdm = backend.assemble(dFdm)

                # Zero out boundary values from boundary conditions as they do not depend (directly) on c.
                for bc in bcs:
                    bc.apply(dFdm)

            elif isinstance(c, backend.DirichletBC):
                #tmp_bc = backend.DirichletBC(V, tlm_value, c_rep.user_sub_domain())
                dFdm = backend.Function(V).vector()
                tlm_value.apply(dFdm)

            elif isinstance(c, backend.Expression):
                dFdm = -backend.derivative(F_form, c_rep, tlm_value)
                dFdm = backend.assemble(dFdm)

                # Zero out boundary values from boundary conditions as they do not depend (directly) on c.
                for bc in bcs:
                    bc.apply(dFdm)

            dudm = Function(V)
            backend.solve(dFdu, dudm.vector(), dFdm)

            fwd_block_output.add_tlm_output(dudm)

    def evaluate_hessian(self):
        # First fetch all relevant values
        fwd_block_output = self.get_outputs()[0]
        adj_input = fwd_block_output.adj_value
        hessian_input = fwd_block_output.hessian_value
        tlm_output = fwd_block_output.tlm_value
        u = fwd_block_output.get_output()
        V = u.function_space()

        # Process the equation forms, replacing values with checkpoints,
        # and gathering lhs and rhs in one single form.
        if self.linear:
            tmp_u = Function(self.func.function_space()) # Replace later? Maybe save function space on initialization.
            F_form = backend.action(self.lhs, tmp_u) - self.rhs
        else:
            tmp_u = self.func
            F_form = self.lhs

        replaced_coeffs = {}
        for block_output in self.get_dependencies():
            coeff = block_output.get_output()
            if coeff in F_form.coefficients():
                replaced_coeffs[coeff] = block_output.get_saved_output()

        replaced_coeffs[tmp_u] = fwd_block_output.get_saved_output()
        F_form = backend.replace(F_form, replaced_coeffs)

        # Define the equation Form. This class is an initial step in refactoring
        # the SolveBlock methods.
        F = Form(F_form, transpose=True)
        F.set_boundary_conditions(self.bcs, fwd_block_output.get_saved_output())

        # Using the equation Form we derive dF/du, d^2F/du^2 * du/dm * direction.
        dFdu = F.derivative(fwd_block_output.get_saved_output())
        d2Fdu2 = dFdu.derivative(fwd_block_output.get_saved_output(), tlm_output)

        # TODO: First-order adjoint solution should be possible to obtain from the earlier adjoint computations.
        adj_sol = backend.Function(V)
        # Solve the (first order) adjoint equation
        backend.solve(dFdu.data, adj_sol.vector(), adj_input)

        # Second-order adjoint (soa) solution
        adj_sol2 = backend.Function(V)

        # Start piecing together the rhs of the soa equation
        b = hessian_input
        b -= d2Fdu2*adj_sol.vector()

        for bo in self.get_dependencies():
            c = bo.get_output()
            c_rep = replaced_coeffs.get(c, c)
            tlm_input = bo.tlm_value

            if c == self.func or tlm_input is None:
                continue

            if not isinstance(c, backend.DirichletBC):
                d2Fdudm = dFdu.derivative(c_rep, tlm_input)
                b -= d2Fdudm*adj_sol.vector()

        # Solve the soa equation
        backend.solve(dFdu.data, adj_sol2.vector(), b)

        # Iterate through every dependency to evaluate and propagate the hessian information.
        for bo in self.get_dependencies():
            c = bo.get_output()
            c_rep = replaced_coeffs.get(c, c)

            if c == self.func and not self.linear:
                continue

            # If m = DirichletBC then d^2F(u,m)/dm^2 = 0 and d^2F(u,m)/dudm = 0,
            # so we only have the term dF(u,m)/dm * adj_sol2
            if isinstance(c, backend.DirichletBC):
                tmp_bc = backend.DirichletBC(V, adj_sol2, *c.domain_args)
                #adj_output = Function(V)
                #tmp_bc.apply(adj_output.vector())

                bo.add_hessian_output([tmp_bc])
                continue

            dFdm = F.derivative(c_rep, function_space=V)
            # TODO: Actually implement split annotations properly.
            try:
                d2Fdudm = dFdu.derivative(c_rep, tlm_output)
            except ufl.log.UFLException:
                continue


            # We need to add terms from every other dependency
            # i.e. the terms d^2F/dm_1dm_2
            for bo2 in self.get_dependencies():
                c2 = bo2.get_output()
                c2_rep = replaced_coeffs.get(c2, c2)

                if isinstance(c2, backend.DirichletBC):
                    continue

                tlm_input = bo2.tlm_value
                if tlm_input is None:
                    continue

                if c2 == self.func and not self.linear:
                    continue

                d2Fdm2 = dFdm.derivative(c2_rep, tlm_input)
                if d2Fdm2.data is None:
                    continue

                output = d2Fdm2*adj_sol.vector()

                if isinstance(c, backend.Expression):
                    bo.add_hessian_output([(-output, V)])
                else:
                    bo.add_hessian_output(-output)

            output = dFdm * adj_sol2.vector()
            output += d2Fdudm*adj_sol.vector()

            if isinstance(c, backend.Expression):
                bo.add_hessian_output([(-output, V)])
            else:
                bo.add_hessian_output(-output)

    def recompute(self):
        func = self.func
        replace_lhs_coeffs = {}
        replace_rhs_coeffs = {}
        for block_output in self.get_dependencies():
            c = block_output.output
            c_rep = block_output.get_saved_output()

            if c != c_rep:
                if c in self.lhs.coefficients():
                    replace_lhs_coeffs[c] = c_rep
                    if c == self.func:
                        func = c_rep
                        block_output.checkpoint = c_rep._ad_create_checkpoint()
                
                if self.linear and c in self.rhs.coefficients():
                    replace_rhs_coeffs[c] = c_rep

        lhs = backend.replace(self.lhs, replace_lhs_coeffs)
        
        rhs = 0
        if self.linear:
            rhs = backend.replace(self.rhs, replace_rhs_coeffs)

        backend.solve(lhs == rhs, func, self.bcs)
        # Save output for use in later re-computations.
        # TODO: Consider redesigning the saving system so a new deepcopy isn't created on each forward replay.
        self.get_outputs()[0].checkpoint = func._ad_create_checkpoint()


class Form(object):
    def __init__(self, form, transpose=False):
        self.form = form
        self.rank = len(form.arguments())
        self.transpose = transpose
        self._data = None

        # Boundary conditions
        self.bcs = None
        self.bc_rows = None
        self.sol_var = None
        self.bc_type = 0

    def derivative(self, coefficient, argument=None, function_space=None):
        dc = argument
        if dc is None:
            if isinstance(coefficient, backend.Constant):
                dc = backend.Constant(1)
            elif isinstance(coefficient, backend.Expression):
                dc = backend.TrialFunction(function_space)

        diff_form = ufl.algorithms.expand_derivatives(backend.derivative(self.form, coefficient, dc))
        ret = Form(diff_form, transpose=self.transpose)
        ret.bcs = self.bcs
        ret.bc_rows = self.bc_rows
        ret.sol_var = self.sol_var

        # Unintuitive way of solving this problem.
        # TODO: Consider refactoring.
        if coefficient == self.sol_var:
            ret.bc_type = self.bc_type + 1
        else:
            ret.bc_type = self.bc_type + 2

        return ret

    def transpose(self):
        transpose = False if self.transpose else True
        return Form(self.form, transpose=transpose)

    def set_boundary_conditions(self, bcs, sol_var):
        self.bcs = []
        self.bc_rows = []
        self.sol_var = sol_var
        for bc in bcs:
            if isinstance(bc, backend.DirichletBC):
                bc = backend.DirichletBC(bc)
                bc.homogenize()
            self.bcs.append(bc)

            for key in bc.get_boundary_values():
                self.bc_rows.append(key)

    def apply_boundary_conditions(self, data):
        import numpy
        if self.bc_type >= 2:
            if self.rank >= 2:
                data.zero(numpy.array(self.bc_rows, dtype=numpy.intc))
            else:
                [bc.apply(data) for bc in self.bcs]
        else:
            [bc.apply(data) for bc in self.bcs]

    @property
    def data(self):
        return self.compute()

    def compute(self):
        if self._data is not None:
            return self._data

        if self.form.empty():
            return None

        data = backend.assemble(self.form)

        # Apply boundary conditions here!
        if self.bcs:
            self.apply_boundary_conditions(data)

        # Transpose if needed
        if self.transpose and self.rank >= 2:
            matrix_mat = backend.as_backend_type(data).mat()
            matrix_mat.transpose(matrix_mat)

        self._data = data
        return self._data

    def __mul__(self, other):
        if self.data is None:
            return 0

        if isinstance(other, Form):
            return self.data*other

        if isinstance(other, dolfin.cpp.la.GenericMatrix):
            if self.rank >= 2:
                return self.data*other
            else:
                # We (almost?) always want Matrix*Vector multiplication in this case.
                return other*self.data
        elif isinstance(other, dolfin.cpp.la.GenericVector):
            if self.rank >= 2:
                return self.data*other
            else:
                return self.data.inner(other)

        # If it reaches this point I have done something wrong.
        return 0
