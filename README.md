# Arx2V - The Self-Aware Arx to Verilog Compiler

Arx2V is the reference compiler for Arx, an intuitive HDL designed for visual architects. It transpiles Arx into clean, synthesizable Verilog-2001.

## Requirements
- Python 3.8 or higher (no external libraries required).
- An EDA tool for simulation/synthesis (Verilator, Yosys, Vivado, etc).

## Philosophy
1. RISC in Structure, CISC in Intrinsics.
2. Fail-Fast (Safe Mode): The compiler refuses to generate Verilog if it detects missing resets, incomplete FSMs, latches, or hardware collisions.
3. No wire vs reg confusion. logic means wire, behavior means reg.

## Features
- Pure Verilog-2001 Output.
- Built-in Testbench Generator.
- Built-in Syntax Reference: Accessible anytime by typing syntaxes inside the compiler REPL.
- Extensive Intrinsics: mux, when, conc, decode, encode, priority encoders, and more.

## Quick Start

1. Enter the REPL:
   arx2v login

2. Inside the REPL, compile a module (file must be in the current directory):
   [arx2v] > compile my_module.arx

3. Inside the REPL, generate a Verilog testbench for it:
   [arx2v] > generate testbench for my_module.arx

## Example (Arx)

~~~arx
module hazard_unit
input:
    ext_wr_en
    ext_wr_addr [5 bits]
    rs1_addr [5 bits]
    rs1_val_in [64 bits]
    ext_wr_data [64 bits]
output:
    rs1_val_out [64 bits]
wires:
    do_bypass
logic:
    connect do_bypass = ext_wr_en logAnd (ext_wr_addr == rs1_addr)
    connect rs1_val_out = mux(do_bypass) rs1_val_in, ext_wr_data
end module
~~~

## License
This project is licensed under the MIT License.
