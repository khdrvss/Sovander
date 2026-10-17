// Willowmere pump controller, made up for a test: the pump runs
// while the tank reads low.
module pump(input wire clk, input wire tank_low, output reg running);
  always @(posedge clk)
    running <= tank_low;
endmodule
