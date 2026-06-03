# 弧形布线命令

## 参考命令
.\layer_assign_cpp.exe -arc 1231_4_arc.txt 1231_input.txt --output layer_input.txt
.\escape_order_cpp.exe layer_input.txt 1231_4_arc.txt
.\arc_main.exe order_input.txt 1231_4_arc.txt constrain.txt

## 参数说明

### layer_assign_cpp.exe   逃逸层分配器
1231_4_arc.txt：整板信息文件，S表达式格式
1231_input.txt：待逃逸器件位号文件，内容为器件位号
layer_input.txt：输出的层分配文件

### escape_order_cpp.exe  逃逸顺序规划器
layer_input.txt：层分配文件
 layer_input.txt：整板信息文件，S表达式格式

### arc_main.exe 布线器
order_input.txt：escape_order_cpp.exe生成并整合的逃逸顺序+逃逸层分配文件
1231_4_arc.txt：整板信息文件，S表达式格式
constrain.txt：约束信息（逃逸前就已提供的文件）