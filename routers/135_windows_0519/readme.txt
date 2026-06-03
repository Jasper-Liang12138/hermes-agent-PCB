# 135布线命令

## 参考命令
.\layer_assign_cpp.exe 402Pin_08BGA_8L_S_01141700.txt 402_input.txt --output layer_input.txt
.\escape_order_cpp.exe layer_input.txt 402Pin_08BGA_8L_S_01141700.txt
.\135_main.exe 402Pin_08BGA_8L_S_01141700.txt order_input.txt 

## 参数说明

### layer_assign_cpp.exe   逃逸层分配器
402Pin_08BGA_8L_S_01141700.txt：整板信息文件，S表达式格式
402_input.txt：待逃逸器件位号文件，内容为器件位号
layer_input.txt：输出的层分配文件

### escape_order_cpp.exe  逃逸顺序规划器
layer_input.txt：层分配文件
402Pin_08BGA_8L_S_01141700.txt：整板信息文件，S表达式格式

### 135_main.exe 布线器
402Pin_08BGA_8L_S_01141700.txt：整板信息文件，S表达式格式
order_input.txt：escape_order_cpp.exe生成并整合的逃逸顺序+逃逸层分配文件
