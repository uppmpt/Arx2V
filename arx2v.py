import re, os, sys, ast

class ArxType:
    def __init__(self, width, signed, is_array=False, size=0):
        self.width, self.signed, self.is_array, self.size = width, signed, is_array, size
    def __repr__(self):
        return f"Array[{self.size}] of {self.width}b" if self.is_array else f"{self.width}b"

class TypeChecker(ast.NodeVisitor):
    def __init__(self, target_type, signal_types):
        self.target_type, self.signal_types = target_type, signal_types
    def visit_Name(self, node):
        if node.id not in self.signal_types: raise ValueError(f"Undeclared signal: '{node.id}'")
        return self.signal_types[node.id]
    def visit_Constant(self, node): return self.target_type
    def visit_BinOp(self, node):
        left, right = self.visit(node.left), self.visit(node.right)
        if left.signed != right.signed: raise ValueError(f"Type mismatch: '{ast.unparse(node)}' mixes signed/unsigned.")
        return ArxType(max(left.width, right.width), left.signed)
    def visit_Compare(self, node):
        left, right = self.visit(node.left), self.visit(node.comparators[0])
        if left.signed != right.signed: raise ValueError(f"Type mismatch in comparison: '{ast.unparse(node)}'.")
        return ArxType(1, False)
    def visit_BoolOp(self, node):
        for v in node.values: self.visit(v)
        return ArxType(1, False)
    def visit_UnaryOp(self, node): return self.visit(node.operand)
    def visit_Call(self, node):
        if isinstance(node.func, ast.Name):
            if node.func.id == "conc":
                return ArxType(sum([self.visit(arg).width for arg in node.args]), False)
            if node.func.id in ["signed", "unsigned"]:
                return ArxType(self.visit(node.args[0]).width, node.func.id == "signed")
            if node.func.id in ["redOr", "redAnd", "redXor"]:
                self.visit(node.args[0])
                return ArxType(1, False)
            if node.func.id == "decode":
                w = self.visit(node.args[0]).width
                return ArxType(2 ** w, False)
            if node.func.id in ["encode", "highPencode", "lowPencode"]:
                w = self.visit(node.args[0]).width
                out_w = max(1, (w - 1).bit_length()) # ceil(log2(w))
                return ArxType(out_w, False)
        return self.target_type

    def visit_Subscript(self, node):
        base_type = self.visit(node.value)
        base_name = node.value.id if isinstance(node.value, ast.Name) else "sinal"
        if base_type.is_array:
            if isinstance(node.slice, ast.Constant):
                if node.slice.value >= base_type.size:
                    raise ValueError(f"Array index [{node.slice.value}] out of bounds for '{base_name}' (size {base_type.size}).")
            return ArxType(base_type.width, base_type.signed)
        if isinstance(node.slice, ast.Constant):
            if node.slice.value >= base_type.width: raise ValueError(f"Index [{node.slice.value}] out of bounds for '{base_name}'")
            return ArxType(1, False)
        elif isinstance(node.slice, ast.Slice):
            hi = node.slice.lower.value if isinstance(node.slice.lower, ast.Constant) else None
            lo = node.slice.upper.value if isinstance(node.slice.upper, ast.Constant) else None
            if hi is not None and lo is not None:
                if hi >= base_type.width or lo >= base_type.width: raise ValueError(f"Slice [{hi}:{lo}] out of bounds.")
                return ArxType(abs(hi - lo) + 1, False)
            return ArxType(base_type.width, False)
        return base_type

def get_lhs_width(lhs, base_type):
    lhs = lhs.strip()
    # Slice top-level: nome[bits X-Y] (nada depois do colchete)
    m = re.match(r"^[\w\.]+\s*\[bits\s+([^\[\]]+)-([^\[\]]+)\]$", lhs)
    if m:
        try:
            return abs(int(m.group(1)) - int(m.group(2))) + 1
        except (ValueError, TypeError):
            pass
    # Bit unico top-level: nome[bit X]
    m = re.match(r"^[\w\.]+\s*\[bit\s+([^\[\]]+)\]$", lhs)
    if m:
        return 1
    # Qualquer outra coisa (array access com indice complexo, nome simples)
    # Retorna a largura do elemento do array (ou do sinal)
    return base_type.width

def translate_lhs(lhs):
    lhs = lhs.replace(".", "_") # Interfaces (bundles)
    lhs = re.sub(r"\[bit\s+([^\]]+)\]", r"[\1]", lhs)
    return re.sub(r"\[bits\s+([^\]]+)-([^\]]+)\]", r"[\1:\2]", lhs)

def translate_expr(expr, target_type, signal_types, line_num=None):
    py_expr = expr.replace(".", "_") # Interfaces (bundles)
    py_expr = py_expr.replace("bitwAnd", "&").replace("bitwOr", "|").replace("bitwXor", "^").replace("bitwNot", "~")
    py_expr = py_expr.replace("logAnd", " and ").replace("logOr", " or ").replace("logNot", " not ")
    py_expr = py_expr.replace("≠", "!=").replace("shf>", ">>").replace("<shf", "<<")
    py_expr = re.sub(r"\[bits\s+([^\]]+)-([^\]]+)\]", r"[\1:\2]", py_expr)
    py_expr = re.sub(r"\[bit\s+([^\]]+)\]", r"[\1]", py_expr)
    try:
        tree = ast.parse(py_expr, mode='eval')
        checker = TypeChecker(target_type, signal_types)
        inferred = checker.visit(tree.body)
        def unparse_verilog(node, parent_type):
            if isinstance(node, ast.Name): return node.id
            if isinstance(node, ast.Subscript):
                base = unparse_verilog(node.value, parent_type)
                if isinstance(node.slice, ast.Constant): return f"{base}[{node.slice.value}]"
                elif isinstance(node.slice, ast.Name): return f"{base}[{node.slice.id}]"
                elif isinstance(node.slice, ast.BinOp): return f"{base}[{unparse_verilog(node.slice, parent_type)}]"
                elif isinstance(node.slice, ast.Slice):
                    hi = str(node.slice.lower.value) if isinstance(node.slice.lower, ast.Constant) else (unparse_verilog(node.slice.lower, parent_type) if node.slice.lower else "")
                    lo = str(node.slice.upper.value) if isinstance(node.slice.upper, ast.Constant) else (unparse_verilog(node.slice.upper, parent_type) if node.slice.upper else "")
                    if hi and lo: return f"{base}[{hi}:{lo}]"
                    elif hi: return f"{base}[{hi}:0]"
                    elif lo: return f"{base}[63:{lo}]"
                return base
            if isinstance(node, ast.Constant):
                return f"{parent_type.width}'sd{node.value}" if parent_type and parent_type.signed else f"{parent_type.width if parent_type else 32}'d{node.value}"
            if isinstance(node, ast.BinOp):
                left, right = unparse_verilog(node.left, parent_type), unparse_verilog(node.right, parent_type)
                op_map = {'Add':'+', 'Sub':'-', 'Mult':'*', 'Div':'/', 'BitAnd':'&', 'BitOr':'|', 'BitXor':'^', 'LShift':'<<', 'RShift':'>>'}
                return f"({left} {op_map.get(type(node.op).__name__, type(node.op).__name__)} {right})"
            if isinstance(node, ast.Compare):
                left_type, right_type = checker.visit(node.left), checker.visit(node.comparators[0])
                if isinstance(node.comparators[0], ast.Constant): right_type = left_type
                if isinstance(node.left, ast.Constant): left_type = right_type
                left, right = unparse_verilog(node.left, left_type), unparse_verilog(node.comparators[0], right_type)
                op_map = {'Eq':'==', 'NotEq':'!=', 'Gt':'>', 'Lt':'<', 'GtE':'>=', 'LtE':'<='}
                return f"({left} {op_map.get(type(node.ops[0]).__name__, type(node.ops[0]).__name__)} {right})"
            if isinstance(node, ast.BoolOp):
                return "(" + (' && ' if isinstance(node.op, ast.And) else ' || ').join([unparse_verilog(v, parent_type) for v in node.values]) + ")"
            if isinstance(node, ast.UnaryOp):
                op_map = {'USub':'-', 'Invert':'~', 'Not':'!'}
                return f"({op_map.get(type(node.op).__name__, '')}{unparse_verilog(node.operand, parent_type)})"
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id == "conc":
                        args_rev = list(reversed(node.args))
                        return "{" + ", ".join([unparse_verilog(arg, parent_type) for arg in args_rev]) + "}"
                    if node.func.id == "signed":
                        return f"$signed({unparse_verilog(node.args[0], parent_type)})"
                    if node.func.id == "unsigned":
                        return f"$unsigned({unparse_verilog(node.args[0], parent_type)})"
                    if node.func.id == "redOr":
                        return f"|{unparse_verilog(node.args[0], parent_type)}"
                    if node.func.id == "redAnd":
                        return f"&{unparse_verilog(node.args[0], parent_type)}"
                    if node.func.id == "redXor":
                        return f"^{unparse_verilog(node.args[0], parent_type)}"
                    if node.func.id == "decode":
                        arg = unparse_verilog(node.args[0], parent_type)
                        return f"(1 << {arg})"
                    if node.func.id in ["encode", "highPencode", "lowPencode"]:
                        arg = unparse_verilog(node.args[0], parent_type)
                        w = checker.visit(node.args[0]).width
                        out_w = max(1, (w - 1).bit_length())
                        items = []
                        if node.func.id in ["encode", "lowPencode"]:
                            for i in range(w):
                                items.append(f"({arg}[{i}] ? {out_w}'d{i} : ")
                        else: # highPencode
                            for i in range(w-1, -1, -1):
                                items.append(f"({arg}[{i}] ? {out_w}'d{i} : ")
                        return "".join(items) + f"{out_w}'d0" + ")" * w
            return ast.unparse(node)
        return unparse_verilog(tree.body, inferred), inferred
    except SyntaxError: raise ValueError(f"{'Line ' + str(line_num) + ': ' if line_num else ''}Invalid expression syntax: '{expr}'")
    except Exception as e: raise ValueError(f"{'Line ' + str(line_num) + ': ' if line_num else ''}Expression error: {e}")

def translate_mux(expr, target_t, signal_types, line_num=None):
    match_mux = re.search(r"mux\((.*?)\)\s*(.*)", expr)
    if not match_mux: return None, []
    sel_raw, options_raw = match_mux.groups()
    sel = translate_expr(sel_raw, ArxType(1, False), signal_types, line_num)[0]
    if "default:" in options_raw:
        raise ValueError("Use 'fallbackVal:' instead of 'default:' in mux.")
    if "default:" in options_raw:
        raise ValueError("Use 'fallbackVal:' instead of 'default:' in mux.")
    default_match = re.search(r"(.*?)\s+fallbackVal:\s*(.*)", options_raw)
    opts = [o.strip() for o in (default_match.group(1).split(',') + [default_match.group(2)])] if default_match else [o.strip() for o in options_raw.split(',')]
    
    mux_warnings = []
    verilog_opts = []
    
    # Smart Coverage Check
    _, sel_type = translate_expr(sel_raw, ArxType(1, False), signal_types)
    total_states = 2 ** sel_type.width
    num_opts = len(opts)
    
    if not default_match and num_opts < total_states:
        mux_warnings.append(f"Mux selector '{sel_raw}' has {sel_type.width} bits ({total_states} states), but only {num_opts} options provided. Last option will be used as implicit fallback.")
    elif default_match and num_opts - 1 < total_states:
        pass # Tem default explicito, mas talvez nao cubra tudo, mas o usuario decidiu o default.

    for opt in opts:
        if re.search(r"\bmux\s*\(", opt):
            raise ValueError(f"{'Line ' + str(line_num) + ': ' if line_num else ''}Nested mux is not supported. Use an intermediate wire.")
        v_opt, t_opt = translate_expr(opt, target_t, signal_types, line_num)
        if t_opt.width > target_t.width:
            mux_warnings.append(f"Truncation in '{opt}' ({t_opt.width} -> {target_t.width}).")
        verilog_opts.append(v_opt)
        
    ternary = verilog_opts[-1]
    for j in range(len(verilog_opts)-2, -1, -1): ternary = f"({sel} == {j}) ? {verilog_opts[j]} : {ternary}"
    return ternary, mux_warnings

def unroll_loops(lines):
    out_lines, i = [], 0
    while i < len(lines):
        line = lines[i]
        match = re.match(r"^(\s*)for\s+(\w+)\s+in\s+(\d+)\.\.(\d+):\s*$", line)
        if match:
            indent, var, start, end = match.groups()
            start, end, body = int(start), int(end), []
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if next_line.strip() == "": body.append(next_line); i += 1; continue
                if len(next_line) - len(next_line.lstrip()) > len(indent): body.append(next_line); i += 1
                else: break
            unrolled_body = unroll_loops(body)
            for val in range(start, end + 1):
                for b_line in unrolled_body: out_lines.append(re.sub(r"\b" + var + r"\b", str(val), b_line))
        else: out_lines.append(line); i += 1
    return out_lines

def merge_continuations(lines):
    out = []
    i = 0
    while i < len(lines):
        current_line = lines[i]
        while i < len(lines) - 1:
            clean = current_line.split('#')[0].strip()
            if not clean: break
            
            next_line = lines[i+1]
            next_clean = next_line.split('#')[0].strip()
            if not next_clean:
                i += 1
                continue
                
            ends_with_op = re.search(r'(\+|-|\*|/|&|\||\^|bitwAnd|bitwOr|bitwXor|logAnd|logOr|shf>|<shf|,|\bdefault:)\s*$', clean)
            unclosed_parens = (clean.count('(') > clean.count(')'))
            is_mux_waiting_args = clean.endswith(')') and 'mux(' in clean and 'fallbackVal:' not in clean
            
            if ends_with_op or unclosed_parens or is_mux_waiting_args:
                i += 1
                current_line = current_line.rstrip() + " " + next_line.strip()
            else:
                break
        out.append(current_line)
        i += 1
    return out

def transpile_single_module(lines):
    lines = merge_continuations(lines)
    lines, warnings = unroll_loops(lines), []
    module_name, inputs, outputs, wires, arrays, logic_verilog, behavior_verilog, extra_functions = "", [], [], [], [], [], [], []
    signal_types, domains, logic_drivers, behavior_drivers, auto_inputs = {}, {}, set(), set(), []
    indent_levels = [-1 if not raw.split("#")[0].strip() else len(raw) - len(raw.lstrip()) for raw in lines]
    current_scan_mode, has_reset_in_behavior, has_assignment_in_behavior, reset_branch_has_assignment, in_reset_if, has_reset_if = None, False, False, False, False, False
    current_block_assignments = set()
    behavior_blocks_assignments = []

    for i, raw_line in enumerate(lines):
        line = raw_line.split("#")[0].strip()
        if not line: continue
        match_width = re.match(r"^([\w\.]+)\s*\[(\d+) bits?\](\s*signed)?(\s*valid\s*\((\d+)\.\.(\d+)\))?$", line)
        if match_width:
            name = match_width.group(1).replace('.', '_')
            width, signed = int(match_width.group(2)), bool(match_width.group(3))
            signal_types[name] = ArxType(width, signed)
            if match_width.group(5) is not None:
                vmin, vmax = int(match_width.group(5)), int(match_width.group(6))
                domains[name] = (vmin, vmax)
                extra_functions.append(f"  wire {name}_invalid;\n  assign {name}_invalid = ({name} < {vmin}) || ({name} > {vmax});" if vmin > 0 else f"  wire {name}_invalid;\n  assign {name}_invalid = ({name} > {vmax});")
                signal_types[f"{name}_invalid"] = ArxType(1, False)
        elif re.match(r"^(\w+)(\s+signed)?$", line):
            name = re.match(r"^(\w+)", line).group(1)
            if name not in ["input", "output", "wires", "logic", "arrays"]: signal_types[name] = ArxType(1, bool(re.search(r"\s+signed", line)))
        elif re.match(r"^(\w+)\s*\[(\d+)\]\s*\[(\d+) bits?\](\s*signed)?$", line):
            match_arr = re.match(r"^(\w+)\s*\[(\d+)\]\s*\[(\d+) bits?\](\s*signed)?$", line)
            signal_types[match_arr.group(1)] = ArxType(int(match_arr.group(3)), bool(match_arr.group(4)), is_array=True, size=int(match_arr.group(2)))

        if line.startswith("behavior on"):
            for sig in re.findall(r"(\w+)\s*\(", re.search(r"behavior on (.*):$", line).group(1)):
                if sig not in signal_types:
                    signal_types[sig] = ArxType(1, False)
                    if sig.lower() not in ["clk", "clock", "clock_n"]: auto_inputs.append(sig)
                if sig.lower() in ["rst", "reset", "rst_n"]: has_reset_in_behavior = True
            current_scan_mode, has_assignment_in_behavior, reset_branch_has_assignment, in_reset_if, has_reset_if = "behavior", False, False, False, False
            current_block_assignments = set()
            behavior_blocks_assignments.append(current_block_assignments)
        elif line == "end behavior":
            if current_scan_mode == "behavior":
                if not has_assignment_in_behavior: raise ValueError("Error: 'behavior' block has no assignments. Register will not be inferred.")
                if has_reset_in_behavior and has_reset_if and not reset_branch_has_assignment: raise ValueError("Error: Reset branch uses 'lock'. Register will not be initialized on reset.")
            current_scan_mode = None
        elif current_scan_mode == "behavior":
            if line.startswith("if"):
                in_reset_if = "rst" in line.lower() or "reset" in line.lower()
                if in_reset_if: has_reset_if = True
            elif line.startswith("else if") or line.startswith("else:"): in_reset_if = False
            if "=" in line and not line.startswith("if") and not line.startswith("else"):
                has_assignment_in_behavior = True
                if in_reset_if: reset_branch_has_assignment = True
                lhs = line.split("=")[0].strip()
                base_lhs = lhs.split("[")[0]
                if re.match(r"^\w+", base_lhs):
                    current_block_assignments.add(base_lhs)
                    behavior_drivers.add(base_lhs)
        elif line.startswith("connect"):
            lhs = line.split("=")[0].replace("connect", "").strip()
            base_lhs = lhs.split("[")[0]
            if base_lhs:
                if base_lhs in logic_drivers:
                    raise ValueError(f"Hardware collision: Multiple 'connect' drivers for '{base_lhs}'.")
                logic_drivers.add(base_lhs)

    # Check for signals assigned in multiple DISTINCT behavior blocks
    seen_signals = set()
    for block in behavior_blocks_assignments:
        for sig in block:
            if sig in seen_signals:
                raise ValueError(f"Hardware collision: '{sig}' driven by multiple 'behavior' blocks.")
            seen_signals.add(sig)

    current_mode, block_stack, instance_mod, instance_name, instance_ports = "global", [], "", "", []
    for i, raw_line in enumerate(lines):
        line_num, line, indent = i + 1, raw_line.split('#')[0].strip(), indent_levels[i]
        if not line or line.startswith("#"): continue

        if line.startswith("module"):
            if len(line.split()) > 1: module_name, current_mode = line.split()[1], "module_declared"
            else: raise ValueError(f"Syntax error on line {line_num}")
        elif line in ["input:", "output:", "wires:", "arrays:", "logic:"]: current_mode = line.replace(":", "")
        elif line.startswith("behavior on"):
            current_mode = "behavior"
            sens_str = re.sub(r"(\w+)\s*\(posSen\)", r"posedge \1", re.search(r"^behavior on (.*):$", line).group(1))
            sens_str = re.sub(r"(\w+)\s*\(negSen\)", r"negedge \1", sens_str)
            sens_str = re.sub(r"(\w+)\s*\(highSen\)", r"\1", sens_str)
            sens_str = re.sub(r"(\w+)\s*\(lowSen\)", r"\1", sens_str)
            behavior_verilog.append(f"always @({sens_str}) begin")
        elif line == "end behavior":
            while block_stack:
                b_type = block_stack[-1]["type"]
                if b_type in ["if", "else_if", "else", "when_branch"]: 
                    behavior_verilog.append("    " + ("  " * (len(block_stack) - 1)) + "end")
                elif b_type == "when":
                    if not block_stack[-1].get("has_fallback", False):
                        warnings.append(f"Warning: FSM 'when' block missing 'fallbackVal' case. State machine may lock up on illegal states.")
                    behavior_verilog.append("    " + ("  " * (len(block_stack) - 1)) + "endcase")
                block_stack.pop()
            behavior_verilog.append("end")
            current_mode = "logic"
        elif line == "end module": current_mode = "done"
        elif line.startswith("instance"):
            match_inst = re.match(r"^instance (\w+) as ([\w\[\]]+):$", line)
            if not match_inst: raise ValueError(f"Error: invalid instance syntax in line {line_num}")
            instance_mod, raw_name = match_inst.groups()
            instance_name = raw_name.replace("[", "_").replace("]", "")
            current_mode, instance_ports = "instance", []
        elif line == "end instance":
            logic_verilog.append(f"  {instance_mod} {instance_name} (\n    {',\n    '.join(instance_ports)}\n  );")
            current_mode = "logic"
        elif current_mode in ["input", "output", "wires"]:
            line = re.sub(r"\s*valid\s*\(\d+\.\.\d+\)|\bsigned\b", "", line)
            match = re.match(r"^([\w\.]+)\s*\[(\d+) bits?\]$", line)
            if match:
                name, bits = match.groups()
                name = name.replace('.', '_')
                sig_prefix = "signed " if signal_types.get(name, ArxType(1, False)).signed else ""
                if current_mode == "input": inputs.append(f"input {sig_prefix}[{int(bits)-1}:0] {name}")
                elif current_mode == "output": outputs.append(f"{'output reg' if name in behavior_drivers else 'output'} {sig_prefix}[{int(bits)-1}:0] {name}")
                elif current_mode == "wires": wires.append(f"{'reg' if name in behavior_drivers else 'wire'} {sig_prefix}[{int(bits)-1}:0] {name};")
            elif re.match(r"^[\w\.]+$", line):
                name = line.replace('.', '_')
                sig_prefix = "signed " if signal_types.get(name, ArxType(1, False)).signed else ""
                if current_mode == "input": inputs.append(f"input {sig_prefix}{name}")
                elif current_mode == "output": outputs.append(f"{'output reg' if name in behavior_drivers else 'output'} {sig_prefix}{name}")
                elif current_mode == "wires": wires.append(f"{'reg' if name in behavior_drivers else 'wire'} {sig_prefix}{name};")
            else: raise ValueError(f"Syntax error on line {line_num}: Unrecognized statement in '{current_mode}' block: '{line}'")
        elif current_mode == "arrays":
            match = re.match(r"^(\w+)\s*\[(\d+)\]\s*\[(\d+) bits?\](\s*signed)?$", line)
            if match: arrays.append(f"reg {'signed ' if match.group(4) else ''}[{int(match.group(3))-1}:0] {match.group(1)} [0:{int(match.group(2))-1}];")
            else: raise ValueError(f"Syntax error on line {line_num}")
        elif current_mode == "instance":
            match = re.match(r"^(\w+)\s*=\s*(.+)$", line)
            if match:
                port_name, port_expr = match.group(1), match.group(2).strip()
                t_type = signal_types.get(port_expr.split('[')[0], ArxType(1, False))
                v_expr, _ = translate_expr(port_expr, t_type, signal_types, line_num)
                instance_ports.append(f".{port_name}({v_expr})")
            else: raise ValueError(f"Syntax error on line {line_num}: Invalid port mapping: '{line}'")
        elif current_mode == "logic":
            match_mux = re.search(r"connect ([\w\[\]\s+\-]+) = mux\(([^)]+)\)\s*(.*)", line)
            if match_mux:
                out_wire, sel, options_raw = match_mux.groups()
                target_t = signal_types.get(out_wire.split("[")[0], ArxType(32, False))
                if target_t.is_array: target_t = ArxType(target_t.width, target_t.signed)
                ternary, mux_warnings = translate_mux(f"mux({sel}) {options_raw}", target_t, signal_types, line_num)
                for w in mux_warnings: warnings.append(f"Warning: Line {line_num}: {w}")
                logic_verilog.append(f"  assign {translate_lhs(out_wire)} = {ternary};")
            else:
                match_connect = re.search(r"connect ([\w\[\]\s+\-]+) = (.*)", line)
                if match_connect:
                    out_wire, expr = match_connect.groups()
                    base_wire = out_wire.split("[")[0]
                    if base_wire in behavior_drivers: raise ValueError(f"Hardware collision on line {line_num}: '{base_wire}' driven by connect and behavior.")
                    target_t = signal_types.get(base_wire, ArxType(32, False))
                    if target_t.is_array: target_t = ArxType(target_t.width, target_t.signed)
                    actual_w = get_lhs_width(out_wire, target_t)
                    target_t = ArxType(actual_w, target_t.signed)
                    v_expr, v_type = translate_expr(expr, target_t, signal_types, line_num)
                    if v_type.width > target_t.width: warnings.append(f"Warning: Line {line_num}: Truncation in '{expr}'.")
                    logic_verilog.append(f"  assign {translate_lhs(out_wire)} = {v_expr};")
                else: raise ValueError(f"Syntax error on line {line_num}: Unrecognized statement in '{current_mode}' block: '{line}'")
        elif current_mode == "behavior":
            while block_stack and block_stack[-1]["indent"] > indent:
                b_type = block_stack[-1]["type"]
                if b_type in ["if", "else_if", "else", "when_branch"]: behavior_verilog.append("    " + ("  " * (len(block_stack) - 1)) + "end")
                elif b_type == "when":
                    if not block_stack[-1].get("has_fallback"):
                        warnings.append(f"Warning: FSM 'when' block missing 'fallbackVal' case. State machine may lock up on illegal states.")
                    behavior_verilog.append("    " + ("  " * (len(block_stack) - 1)) + "endcase")
                block_stack.pop()
            if not line.startswith("else"):
                while block_stack and block_stack[-1]["indent"] == indent and block_stack[-1]["type"] in ["if", "else_if", "else"]:
                    behavior_verilog.append("    " + ("  " * (len(block_stack) - 1)) + "end")
                    block_stack.pop()

            is_branch = bool(re.match(r"^(\w+)\s*:", line)) and not line.startswith(("if ", "else", "when ", "for "))
            if is_branch and block_stack and block_stack[-1]["type"] == "when_branch":
                behavior_verilog.append("    " + ("  " * (len(block_stack) - 1)) + "end")
                block_stack.pop()

            if line.startswith("when "):
                v_cond, v_type = translate_expr(line.replace('when ', '').rstrip(':'), ArxType(1, False), signal_types, line_num)
                behavior_verilog.append(f"{'    ' + '  ' * len(block_stack)}case ({v_cond[1:-1] if v_cond.startswith('(') and v_cond.endswith(')') else v_cond})")
                block_stack.append({"type": "when", "indent": indent, "width": v_type.width, "has_fallback": False})
            elif is_branch and block_stack and block_stack[-1]["type"] == "when":
                match_branch = re.match(r"^(\w+)\s*:(.*)$", line)
                branch_val, branch_body = match_branch.group(1), match_branch.group(2).strip()
                if branch_val == "fallbackVal":
                    block_stack[-1]["has_fallback"] = True
                base_indent = "    " + ("  " * len(block_stack))
                if branch_val == "fallbackVal":
                    block_stack[-1]["has_fallback"] = True
                if branch_body:
                    behavior_verilog.append(f"{base_indent}{'default' if branch_val == 'fallbackVal' else branch_val}: begin")
                    t_sig, t_expr = branch_body.split("=", 1)
                    t_sig, t_expr = t_sig.strip(), t_expr.strip()
                    target_t = signal_types.get(t_sig.split('[')[0], ArxType(32, False))
                    if target_t.is_array: target_t = ArxType(target_t.width, target_t.signed)
                    if t_expr.startswith("mux("):
                        v_expr, mux_warnings = translate_mux(t_expr, target_t, signal_types, line_num)
                        for w in mux_warnings: warnings.append(f"Warning: Line {line_num}: {w}")
                    else:
                        v_expr, v_type = translate_expr(t_expr, target_t, signal_types, line_num)
                        if v_type.width > target_t.width:
                            warnings.append(f"Warning: Line {line_num}: Truncation in '{t_expr}' ({v_type.width} -> {target_t.width}).")
                    behavior_verilog.append(f"      {translate_lhs(t_sig)} <= {v_expr};")
                    behavior_verilog.append(f"{base_indent}end")
                else:
                    behavior_verilog.append(f"{base_indent}{'default' if branch_val == 'fallbackVal' else branch_val}: begin")
                    block_stack.append({"type": "when_branch", "indent": indent})
            elif line.startswith("if"):
                v_cond, _ = translate_expr(line.replace('if ', '').rstrip(':'), ArxType(1, False), signal_types, line_num)
                behavior_verilog.append(f"{'    ' + '  ' * len(block_stack)}if ({v_cond[1:-1] if v_cond.startswith('(') and v_cond.endswith(')') else v_cond}) begin")
                block_stack.append({"type": "if", "indent": indent})
            elif line.startswith("else if"):
                if not block_stack or block_stack[-1]["indent"] != indent: raise ValueError(f"Error: 'else if' without matching 'if' in line {line_num}")
                depth = len(block_stack) - 1
                v_cond, _ = translate_expr(line.replace('else if ', '').rstrip(':'), ArxType(1, False), signal_types, line_num)
                behavior_verilog.append(f"{'    ' + '  ' * depth}end else if ({v_cond[1:-1] if v_cond.startswith('(') and v_cond.endswith(')') else v_cond}) begin")
                block_stack[-1] = {"type": "else_if", "indent": indent}
            elif line.startswith("else:"):
                if not block_stack or block_stack[-1]["indent"] != indent: raise ValueError(f"Error: 'else' without matching 'if' in line {line_num}")
                depth = len(block_stack) - 1
                behavior_verilog.append(f"{'    ' + '  ' * depth}end else begin")
                block_stack[-1] = {"type": "else", "indent": indent}
            elif line.startswith("lock"): behavior_verilog.append("    " + ("  " * len(block_stack)) + "// lock")
            elif "=" in line:
                target_sig, expr = line.split("=", 1)
                target_sig, expr = target_sig.strip(), expr.strip()
                base_sig = target_sig.split("[")[0]
                if base_sig in logic_drivers: raise ValueError(f"Hardware collision on line {line_num}: '{base_sig}' driven by connect and behavior.")
                target_t = signal_types.get(base_sig, ArxType(32, False))
                if target_t.is_array: target_t = ArxType(target_t.width, target_t.signed)
                actual_w = get_lhs_width(target_sig, target_t)
                target_t = ArxType(actual_w, target_t.signed)
                if expr.startswith("mux("):
                    v_expr, mux_warnings = translate_mux(expr, target_t, signal_types, line_num)
                    for w in mux_warnings: warnings.append(f"Warning: Line {line_num}: {w}")
                else:
                    v_expr, v_type = translate_expr(expr, target_t, signal_types, line_num)
                    if v_type.width > target_t.width:
                        warnings.append(f"Warning: Line {line_num}: Truncation in '{expr}' ({v_type.width} -> {target_t.width}).")
                behavior_verilog.append("    " + ("  " * len(block_stack)) + f"{translate_lhs(target_sig)} <= {v_expr};")

    for sig in auto_inputs:
        if not any(sig in inp.split() for inp in inputs): inputs.append(f"input {sig}")
    verilog = f"module {module_name} (\n" + ",\n".join(["  " + i for i in inputs + outputs]) + "\n);\n\n"
    for arr in arrays: verilog += f"  {arr}\n"
    for w in wires: verilog += f"  {w}\n"
    for f in extra_functions: verilog += f"{f}\n\n"
    for l in logic_verilog: verilog += f"{l}\n"
    verilog += "\n"
    for b in behavior_verilog: verilog += f"{b}\n"
    return verilog + "endmodule\n", warnings

def transpile_to_verilog(source_code):
    lines, modules, current_module = source_code.split('\n'), [], []
    for line in lines:
        if line.strip().startswith("module") and current_module: modules.append(current_module); current_module = []
        current_module.append(line)
    if current_module: modules.append(current_module)
    out_verilog, all_warnings = [], []
    for mod_lines in modules:
        if any(l.strip() for l in mod_lines):
            v_code, w_list = transpile_single_module(mod_lines)
            out_verilog.append(v_code); all_warnings.extend(w_list)
    return "\n".join(out_verilog), all_warnings

def handle_compilation(source):
    try:
        verilog_code, warnings = transpile_to_verilog(source)
        if warnings:
            for w in warnings: print(w, file=sys.stderr)
            print("\n--- WARNINGS DETECTED ---", file=sys.stderr)
            if input("Generation paused due to warnings. Generate code anyway? (Yes/No): ").strip().lower() not in ["yes", "y"]:
                print("Compilation aborted by user.", file=sys.stderr); return
        print(verilog_code)
    except Exception as e: print(f"Compilation error: {e}", file=sys.stderr)
def validate_line(line, context):
    clean = line.split("#")[0].strip()
    if not clean: return True, context
    if clean in ["input:", "output:", "wires:", "arrays:", "logic:"]: return True, clean.replace(":", "")
    if clean == "end module": return True, "done"
    if clean.startswith("behavior on"): return (True, "behavior") if re.match(r"^behavior on .+:$", clean) else (False, context)
    if clean == "end behavior": return True, "logic"
    if clean.startswith("instance"): return (True, "instance") if re.match(r"^instance \w+ as \w+(\[\w+\])?:$", clean) else (False, context)
    if clean == "end instance": return True, "logic"
    if context == "global": return (True, "module_declared") if re.match(r"^module \w+$", clean) else (False, context)
    if context == "module_declared": return False, context
    if context in ["input", "output", "wires"]: return (True, context) if re.match(r"^\w+(\s*\[\d+\s*bits?\])?(\s*signed)?(\s*valid\s*\(\d+\.\.\d+\))?$", clean) else (False, context)
    if context == "arrays": return (True, context) if re.match(r"^\w+\s*\[\d+\]\s*\[\d+\s*bits?\](\s*signed)?$", clean) else (False, context)
    if context == "instance": return (True, context) if re.match(r"^\w+\s*=\s*\w+$", clean) else (False, context)
    if context == "logic":
        if clean.startswith("connect ") and "=" in clean: return True, context
        if re.match(r"^for \w+ in \d+\.\.\d+:$", clean): return True, context
        return False, context
    if context == "behavior":
        if re.match(r"^(if|else if|when) .+:$", clean) or clean in ["else:", "lock"]: return True, context
        if re.match(r"^(\w+)\s*:.*$", clean): return True, context
        if re.match(r"^\w+(\[\w+\])?\s*=\s*.+$", clean): return True, context 
        if re.match(r"^for \w+ in \d+\.\.\d+:$", clean): return True, context
        return False, context
    return False, context


def generate_tb_from_source(source):
    lines = source.split('\n')
    modules = re.findall(r"module (\w+)", source)
    if not modules: raise ValueError("No module found in the file.")
    dut_name = modules[-1]
    
    inputs_tb, outputs_tb = [], []
    seen_in, seen_out = set(), set()
    mode = None
    
    for line in lines:
        line = line.split('#')[0].strip()
        if line == "input:": mode = "in"
        elif line == "output:": mode = "out"
        elif line in ["wires:", "logic:", "arrays:", "behavior on", "interfaces:"] or line.startswith("end module"): mode = None
        elif mode in ["in", "out"] and line:
            match = re.match(r"^([\w\.]+)(?:\s*\[(\d+)\s*bits?\])?", line)
            if match:
                name, width = match.group(1).replace(".", "_"), match.group(2)
                w_str = f"[{int(width)-1}:0] " if width else ""
                if mode == "in" and name not in seen_in:
                    inputs_tb.append((name, w_str))
                    seen_in.add(name)
                elif mode == "out" and name not in seen_out:
                    outputs_tb.append((name, w_str))
                    seen_out.add(name)

    tb = "`timescale 1ns/1ps\n\n"
    tb += f"module tb_{dut_name};\n"
    for name, w in inputs_tb:
        if name == "clk": tb += f"  reg clk;\n"
        else: tb += f"  reg {w}{name};\n"
    for name, w in outputs_tb: tb += f"  wire {w}{name};\n"
    tb += f"\n  {dut_name} dut (\n"
    for name, _ in inputs_tb: tb += f"    .{name}({name}),\n"
    for name, _ in outputs_tb: tb += f"    .{name}({name}),\n"
    tb = tb.rstrip(",\n") + "\n  );\n\n"
    
    if any(n == "clk" for n, _ in inputs_tb):
        tb += "  always #5 clk = ~clk;\n\n"
        
    tb += "  initial begin\n"
    for name, _ in inputs_tb:
        if name != "clk": tb += f"    {name} = 0;\n"
    if any(n == "clk" for n, _ in inputs_tb):
        tb += "    clk = 0;\n"
    
    if any(n == "rst" for n, _ in inputs_tb):
        tb += "    #10 rst = 0;\n    #10 rst = 1;\n"
    
    tb += "\n    // Write your test stimuli here\n    #100;\n    $display(\"Simulation finished successfully!\");\n    $finish;\n  end\n"
    tb += "endmodule\n"
    return dut_name, tb


def handle_tb_generation(filename):
    found_file = find_arx_file(filename)
    if not found_file:
        print(f"Error: File '{filename}' not found in current dir or tests/.", file=sys.stderr)
        return
    filename = found_file
    with open(filename, "r") as f: source = f.read()
    try:
        dut_name, tb_code = generate_tb_from_source(source)
        tb_filename = f"tb_{dut_name}.v"
        with open(tb_filename, "w") as f: f.write(tb_code)
        print(f"--- TESTBENCH GENERATED: {tb_filename} ---", file=sys.stderr)
        print(tb_code)
    except Exception as e:
        print(f"Testbench generation error: {e}", file=sys.stderr)


def find_arx_file(filename):
    if os.path.exists(filename): return filename
    paths = [
        os.path.join("tests", filename),
        os.path.join(os.path.expanduser("~"), "arx_compiler", "tests", filename),
        os.path.join(os.path.expanduser("~"), "arx_compiler", filename),
    ]
    for p in paths:
        if os.path.exists(p): return p
    return None

def generate_cocotb_from_source(source):
    modules = re.findall(r"module (\w+)", source)
    if not modules: raise ValueError("No module found.")
    dut_name = modules[-1]
    inputs_tb = []
    mode = None
    for line in source.split('\n'):
        line = line.split('#')[0].strip()
        if line == "input:": mode = "in"
        elif line in ["output:", "wires:", "logic:", "arrays:", "behavior on", "interfaces:"] or line.startswith("end module"): mode = None
        elif mode == "in" and line:
            match = re.match(r"^(\w+)(?:\s*\[(\d+)\s*bits?\])?", line)
            if match: inputs_tb.append(match.group(1).replace(".", "_"))

    py_code = "import cocotb\n"
    py_code += "from cocotb.clock import Clock\n"
    py_code += "from cocotb.triggers import RisingEdge, Timer\n\n"
    py_code += f"@cocotb.test()\n"
    py_code += f"async def test_{dut_name}(dut):\n"
    py_code += f"    clock = Clock(dut.clk, 10, units='ns')\n"
    py_code += f"    cocotb.start_soon(clock.start())\n\n"
    py_code += f"    dut.rst.value = 0\n"
    py_code += f"    await Timer(20, units='ns')\n"
    py_code += f"    dut.rst.value = 1\n\n"
    py_code += f"    # Write your test stimuli here\n"
    for i in inputs_tb:
        if i not in ["clk", "rst"]:
            py_code += f"    dut.{i}.value = 0\n"
    py_code += f"\n    await Timer(100, units='ns')\n"
    py_code += f"    dut._log.info('Simulation finished successfully!')\n"
    return dut_name, py_code


SYNTAX_REFERENCE = """=== ARX2V LANGUAGE REFERENCE ===

STRUCTURE
  module Name
  input: / output: / wires: / arrays: / interfaces:
  logic:          combinational
  behavior on clk (posSen) or rst (negSen):   sequential
  end module

TYPES
  name [N bits]                 unsigned
  name [N bits] signed          signed
  name [N bits] valid (a..b)    domain check -> auto <name>_invalid
  name [SIZE] [N bits]          array / BRAM

SENSITIVITY (behavior on)
  (posSen)   rising edge
  (negSen)   falling edge
  (highSen)  level high
  (lowSen)   level low

OPERATORS
  bitwAnd / bitwOr / bitwXor / bitwNot     bitwise (&, |, ^, ~)
  logAnd / logOr / logNot                  logical (&&, ||, !)
  shf> N / <shf N                          shift right / left

SLICES
  sig[bit N]              single bit
  sig[bits A-B]           range A down to B

LOGIC
  connect out = expr
  connect out = mux(sel) a, b, c fallbackVal: 0
  instance Mod as name:
    port = signal
  end instance

SEQUENTIAL
  if / else if / else:
  lock                                   hold previous value
  when state:                            FSM (generates case)
    0: ...
    fallbackVal: ...
  for i in 0..N:                         loop unroll

BUILT-INS
  conc(lsb, ..., msb)                    concatenation (LSB first)
  signed(x) / unsigned(x)                cast
  redOr / redAnd / redXor                reduction
  decode(sel)                            one-hot decode
  encode / highPencode / lowPencode      priority encoder

COMMANDS
  compile <file.arx>                     compile to Verilog
  generate testbench for <file.arx>      generate Verilog testbench
  syntaxes                                this reference
  exit                                   quit

TIP: All signals must be declared before use."""

def start_repl():
    print("Welcome to Arx2V compiler v1.0")
    print("Commands: compile <file> | generate testbench for <file> | syntaxes | exit")
    print()
    while True:
        try:
            cmd_clean = input("[arx2v] > ").strip()
            if not cmd_clean:
                continue
            lower = cmd_clean.lower()
            if lower in ["exit", "quit"]:
                print("Goodbye!")
                break
            if lower == "syntaxes":
                print(SYNTAX_REFERENCE)
                continue
            if lower.startswith("compile "):
                filename = cmd_clean.split(" ", 1)[1].strip()
                if not os.path.exists(filename):
                    print(f"Error: file '{filename}' not found.", file=sys.stderr)
                    continue
                with open(filename, "r") as f:
                    source = f.read()
                print(f"\n--- COMPILING {filename} ---", file=sys.stderr)
                handle_compilation(source)
                continue
            if lower.startswith("generate testbench for "):
                filename = cmd_clean[len("generate testbench for "):].strip()
                handle_tb_generation(filename)
                continue
            print(f"Error: invalid command '{cmd_clean}'. Type 'syntaxes' for available commands.")
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)

def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "login":
        start_repl()
    elif len(sys.argv) > 1 and sys.argv[1].lower() == "compile":
        filename = sys.argv[2]
        if os.path.exists(filename):
            with open(filename, "r") as f: source = f.read()
            print(f"--- COMPILING {filename} ---", file=sys.stderr); handle_compilation(source)
        else: print(f"Error: File '{filename}' not found.", file=sys.stderr)
    elif len(sys.argv) > 1:
        filename = sys.argv[1]
        if os.path.exists(filename):
            with open(filename, "r") as f: source = f.read()
            print(f"--- COMPILING {filename} ---", file=sys.stderr); handle_compilation(source)
        else: print(f"Error: File '{filename}' not found.", file=sys.stderr)
    else: start_repl()

if __name__ == "__main__":
    main()
