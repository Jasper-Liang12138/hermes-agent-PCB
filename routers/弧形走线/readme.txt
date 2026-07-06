.\layer_assign_cpp.exe -arc 1231_4_arc.txt 1231_input.txt --output layer_input.txt
.\escape_order_cpp.exe layer_input.txt 1231_4_arc.txt
.\arc_main.exe order_input.txt 1231_4_arc.txt constrain.txt
python Turn_QYF.py 1231_4_arc.txt ARC_output.txt 1234_1_arc_output.txt

.\layer_assign_cpp.exe -arc ckb2.txt ckb2_input.txt --output layer_input.txt
.\escape_order_cpp.exe layer_input.txt ckb2.txt
.\arc_main.exe order_input.txt ckb2.txt constrain.txt
python Turn_QYF.py ckb2.txt ARC_output.txt QTF_ckb2_output.txt