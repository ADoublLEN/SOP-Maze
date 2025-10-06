import json
import re
from jsonschema import validate
from jsonschema.exceptions import ValidationError
import json_repair
from json_repair import repair_json
import sys
import os
import glob
from pathlib import Path

def txt_to_json(text):
    good_json_string = repair_json(text, ensure_ascii=False)
    decoded_object = json_repair.loads(good_json_string)
    return decoded_object

def normalize_string(s):
    """标准化字符串用于比较"""
    if not isinstance(s, str):
        return s
    # 统一大小写
    s = s.lower()
    # 移除多余空格
    s = re.sub(r'\s+', ' ', s.strip())
    # 可以添加更多标准化规则
    return s

def validate_model_response(
    model_response,
    target,
    required_json_schema,
    unordered_list_keys=None,
    no_required_eval_acc_keys=None
):
    """
    验证模型响应的格式和内容

    Args:
        model_response: 模型的原始响应文本
        target: 目标答案（字典格式）
        required_json_schema: 要求的JSON schema
        unordered_list_keys: 需要进行无序比较的列表键名集合，如果为None则所有列表都按顺序比较
        no_required_eval_acc_keys: 不参与内容比对（eval acc）的键名集合；若为 None 则比较所有键。
            注意：按"键名匹配"，在任意层级遇到该键名即跳过内容比较（schema 仍会校验）。

    Returns:
        tuple: (score, message, detailed_errors)
            - score: 1.0 (完全正确), 0.2 (格式正确但内容不一致), 0 (格式错误)
            - message: 详细的验证信息
            - detailed_errors: 具体的错误详情列表
    """

    # 标准化集合类型
    if unordered_list_keys is not None and not isinstance(unordered_list_keys, (set, list, tuple)):
        unordered_list_keys = set([unordered_list_keys])
    if isinstance(unordered_list_keys, (list, tuple)):
        unordered_list_keys = set(unordered_list_keys)

    if no_required_eval_acc_keys is not None and not isinstance(no_required_eval_acc_keys, (set, list, tuple)):
        no_required_eval_acc_keys = set([no_required_eval_acc_keys])
    if isinstance(no_required_eval_acc_keys, (list, tuple)):
        no_required_eval_acc_keys = set(no_required_eval_acc_keys)

    # 第一步：将文本转换为JSON格式
    try:
        model_response_json = txt_to_json(model_response)
    except Exception as e:
        return 0, f"❌ 文本转JSON失败: {str(e)}", []

    # 第二步：验证JSON格式是否符合schema
    try:
        validate(instance=model_response_json, schema=required_json_schema)
    except ValidationError as e:
        error_path = " -> ".join([str(p) for p in e.absolute_path]) if e.absolute_path else "根级别"
        schema_error = {
            "type": "schema_validation",
            "path": error_path,
            "message": e.message,
            "failed_value": getattr(e, 'instance', None)
        }
        return 0, f"❌ 格式验证失败: 在路径 '{error_path}' 处，{e.message}", [schema_error]
    except Exception as e:
        return 0, f"❌ Schema验证异常: {str(e)}", []

    # 第三步：格式正确，比较内容是否一致
    differences = []
    detailed_errors = []

    def should_skip_by_keyname(key_name: str) -> bool:
        """是否根据键名跳过内容比对（任意层级）"""
        return bool(no_required_eval_acc_keys and key_name in no_required_eval_acc_keys)

    def create_comparable_item(item):
        """创建忽略指定键的item副本，用于比较"""
        if isinstance(item, dict):
            comparable = {}
            for k, v in item.items():
                if should_skip_by_keyname(k):
                    continue
                comparable[k] = create_comparable_item(v)
            return comparable
        elif isinstance(item, list):
            return [create_comparable_item(sub_item) for sub_item in item]
        else:
            return item
    
    def compare_nested(response_data, target_data, path=""):
        """递归比较嵌套的数据结构"""
        current_key = path.split('.')[-1] if path else ""

        # 跳过不需要验证的key
        if should_skip_by_keyname(current_key):
            return

        if isinstance(target_data, dict) and isinstance(response_data, dict):
            for key in target_data.keys():
                current_path = f"{path}.{key}" if path else key

                # 对子键也做"按键名跳过"判断
                if should_skip_by_keyname(key):
                    continue

                target_value = target_data[key]
                if key not in response_data:
                    error_detail = {
                        "type": "missing_key",
                        "path": current_path,
                        "expected_value": target_value,
                        "actual_value": None
                    }
                    differences.append(f"缺少字段 '{current_path}'")
                    detailed_errors.append(error_detail)
                else:
                    compare_nested(response_data[key], target_value, current_path)

        elif isinstance(target_data, list) and isinstance(response_data, list):
            # 如果该列表键名需要跳过，则不做内容比对
            if should_skip_by_keyname(current_key):
                return

            # 对所有列表都进行无序比较
            if len(response_data) != len(target_data):
                error_detail = {
                    "type": "list_length_mismatch",
                    "path": path,
                    "expected_length": len(target_data),
                    "actual_length": len(response_data)
                }
                differences.append(f"'{path}' 列表长度不匹配: 期望 {len(target_data)}, 实际 {len(response_data)}")
                detailed_errors.append(error_detail)
            else:
                # 过滤掉不需要比较的字段后再进行无序比较
                def filter_dict_for_comparison(item):
                    """过滤掉不需要比较的字段"""
                    if isinstance(item, dict):
                        filtered = {}
                        for k, v in item.items():
                            if not should_skip_by_keyname(k):
                                filtered[k] = v
                        return filtered
                    return item

                # 对列表中的元素进行过滤和无序比较
                filtered_response = [filter_dict_for_comparison(item) for item in response_data]
                filtered_target = [filter_dict_for_comparison(item) for item in target_data]
                
                # 使用集合进行无序比较，确保对象内部键也按字典序排列
                def normalize_for_comparison(item):
                    """标准化数据结构用于无序比较"""
                    if isinstance(item, dict):
                        result = {}
                        for k, v in item.items():
                            result[k] = normalize_for_comparison(v)
                        return result
                    elif isinstance(item, list):
                        # 对数组元素进行标准化后排序
                        normalized_items = [normalize_for_comparison(sub_item) for sub_item in item]
                        # 使用JSON字符串作为排序键
                        return sorted(normalized_items, key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))
                    else:
                        return item

                # 在比较时使用
                normalized_response = [normalize_for_comparison(item) for item in filtered_response]
                normalized_target = [normalize_for_comparison(item) for item in filtered_target]

                response_set = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in normalized_response}
                target_set = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in normalized_target}

                if response_set != target_set:
                    error_detail = {
                        "type": "unordered_list_content_mismatch",
                        "path": path,
                        "expected_items": filtered_target,
                        "actual_items": filtered_response
                    }
                    excluded_fields = list(no_required_eval_acc_keys or [])
                    excluded_msg = f"，已排除 {excluded_fields} 字段" if excluded_fields else ""
                    differences.append(f"'{path}' 列表内容不匹配（无序比较{excluded_msg}）")
                    detailed_errors.append(error_detail)

        else:
            # 基本类型比较 - 添加字符串标准化
            def normalize_string(s):
                """标准化字符串用于比较"""
                if not isinstance(s, str):
                    return s
                # 统一大小写
                s = s.lower()
                # 移除多余空格
                s = re.sub(r'\s+', ' ', s.strip())
                # 可以添加更多标准化规则，比如：
                # s = s.replace('app', 'APP')  # 特定词汇标准化
                return s
            
            if isinstance(response_data, str) and isinstance(target_data, str):
                # 对字符串进行标准化比较
                if normalize_string(response_data) != normalize_string(target_data):
                    error_detail = {
                        "type": "value_mismatch",
                        "path": path,
                        "expected_value": target_data,
                        "actual_value": response_data
                    }
                    differences.append(f"'{path}' 值不匹配: 期望 '{target_data}', 实际 '{response_data}'")
                    detailed_errors.append(error_detail)
            else:
                # 非字符串的严格比较
                if response_data != target_data:
                    error_detail = {
                        "type": "value_mismatch",
                        "path": path,
                        "expected_value": target_data,
                        "actual_value": response_data
                    }
                    differences.append(f"'{path}' 值不匹配: 期望 '{target_data}', 实际 '{response_data}'")
                    detailed_errors.append(error_detail)
    
    # 执行比较（仅对内容做 acc 比对；schema 验证已在上方完成）
    compare_nested(model_response_json, target)

    # 根据比较结果返回
    if not differences:
        return 1, "✅ Correct", []
    else:
        error_details = "; ".join(differences)
        return 0.2, f"🔶 格式正确，但内容不一致: {error_details}", detailed_errors


def process_single_file(data_path, log_file_path):
    """处理单个JSON文件并生成对应的日志"""
    
    # 重定向stdout到文件，同时保留控制台输出
    class Tee:
        def __init__(self, *files):
            self.files = files
        def write(self, obj):
            for f in self.files:
                f.write(obj)
                f.flush()
        def flush(self):
            for f in self.files:
                f.flush()

    # 打开日志文件
    with open(log_file_path, 'w', encoding='utf-8') as f:
        # 创建同时输出到文件和控制台的对象
        original_stdout = sys.stdout
        sys.stdout = Tee(sys.stdout, f)
        
        try:
            # 加载数据文件
            with open(data_path, "r", encoding="utf-8") as data_file:
                data = json.load(data_file)
        
            print("=" * 80)
            print(f"📊 模型响应验证结果 - {os.path.basename(data_path)}")
            print("=" * 80)

            total_items = len(data)
            correct_count = 0
            partial_count = 0
            error_count = 0

            for idx, item in enumerate(data, 1):
                # 读取"需要排除的键名集合"
                no_required_eval_acc_keys = item.get("no_required_eval_acc_keys", None)

                # 读取"无序比较列表键名集合"（保持原有功能）
                unordered_list_keys = item.get("unordered_list_keys", None)

                score, exp, detailed_errors = validate_model_response(
                    item["model_response"],
                    item["target"],
                    item["json_schema"],
                    unordered_list_keys=unordered_list_keys,
                    no_required_eval_acc_keys=no_required_eval_acc_keys
                )

                # 统计结果
                if score == 1:
                    correct_count += 1
                    status_icon = "✅"
                elif score == 0.2:
                    partial_count += 1
                    status_icon = "🔶"
                else:
                    error_count += 1
                    status_icon = "❌"

                print(f"\n{'-' * 60}")
                print(f"📝 测试项目 {idx}/{total_items} {status_icon}")
                print(f"{'-' * 60}")

                # 显示验证范围信息
                if no_required_eval_acc_keys:
                    print(f"🔍 比对范围: 排除键 {list(no_required_eval_acc_keys)}（任意层级按键名跳过）")
                else:
                    print("🔍 比对范围: 比较所有键")

                # 显示模型响应（格式化）
                try:
                    model_json = txt_to_json(item["model_response"])
                    print("🤖 模型响应:")
                    print(json.dumps(model_json, ensure_ascii=False, indent=2))
                except Exception as e:
                    print(f"🤖 模型响应 (原始): {item['model_response']}")
                    print(f"⚠️  JSON解析失败: {e}")

                # 显示目标答案（格式化）
                print("\n🎯 目标答案:")
                print(json.dumps(item["target"], ensure_ascii=False, indent=2))

                # 显示验证结果
                print(f"\n📊 验证结果: 分数 {score}")
                print(f"💬 详细信息: {exp}")

                # 显示具体的错误详情
                if detailed_errors:
                    print("\n🔍 具体错误详情:")
                    for i, error in enumerate(detailed_errors, 1):
                        print(f"  {i}. 错误类型: {error['type']}")
                        print(f"     路径: {error['path']}")
                        if error['type'] == 'missing_key':
                            print(f"     问题: 缺少必需的key")
                            print(f"     期望值: {error['expected_value']}")
                        elif error['type'] == 'extra_key':
                            print(f"     问题: 存在多余的key")
                            print(f"     实际值: {error['actual_value']}")
                        elif error['type'] == 'value_mismatch':
                            print(f"     问题: key存在但值不匹配")
                            print(f"     期望值: {error['expected_value']}")
                            print(f"     实际值: {error['actual_value']}")
                        elif error['type'] == 'list_length_mismatch':
                            print(f"     问题: 列表长度不匹配")
                            print(f"     期望长度: {error['expected_length']}")
                            print(f"     实际长度: {error['actual_length']}")
                        elif error['type'] == 'unordered_list_content_mismatch':
                            print(f"     问题: 无序列表内容不匹配")
                            if 'expected_items' in error:
                                print(f"     期望项: {error['expected_items']}")
                                print(f"     实际项: {error['actual_items']}")
                        elif error['type'] == 'schema_validation':
                            print(f"     问题: Schema验证失败")
                            print(f"     错误信息: {error['message']}")
                            if error.get('failed_value', None) is not None:
                                print(f"     失败的值: {error['failed_value']}")
                        print()

                # 如果是部分正确或错误，显示更多细节
                if score != 1:
                    print(f"🔍 问题分析: {exp}")

            # 显示总体统计
            print("\n" + "=" * 80)
            print("📈 总体统计结果")
            print("=" * 80)
            print(f"📊 总测试项目: {total_items}")
            print(f"✅ 完全正确: {correct_count} ({correct_count/total_items*100:.1f}%)")
            print(f"🔶 格式正确但内容不一致: {partial_count} ({partial_count/total_items*100:.1f}%)")
            print(f"❌ 格式错误: {error_count} ({error_count/total_items*100:.1f}%)")
            print(f"🎯 总体准确率: {(correct_count + partial_count*0.2)/total_items*100:.1f}%")
            print("=" * 80)
            
            return {
                'file_name': os.path.basename(data_path),
                'total_items': total_items,
                'correct_count': correct_count,
                'partial_count': partial_count,
                'error_count': error_count,
                'accuracy': (correct_count + partial_count*0.2)/total_items*100
            }
            
        except Exception as e:
            print(f"❌ 处理文件 {data_path} 时出错: {str(e)}")
            return None
            
        finally:
            # 恢复原始stdout
            sys.stdout = original_stdout


if __name__ == "__main__":
    # 设置目录路径

    current_dir = Path(__file__).parent
    # 构建相对路径
    

    data_directory = current_dir.parent / "data_with_model_response"
    log_directory = current_dir.parent / "results" 
    
    # 确保日志目录存在
    os.makedirs(log_directory, exist_ok=True)
    
    # 获取所有JSON文件
    json_files = glob.glob(os.path.join(data_directory, "*.json"))
    
    if not json_files:
        print(f"❌ 在目录 {data_directory} 中没有找到JSON文件")
        sys.exit(1)
    
    print(f"🔍 找到 {len(json_files)} 个JSON文件")
    
    # 存储所有文件的统计结果
    all_results = []
    
    # 处理每个JSON文件
    for json_file in sorted(json_files):
        file_name = os.path.basename(json_file)
        log_file_name = os.path.splitext(file_name)[0] + "_validation.log"
        log_file_path = os.path.join(log_directory, log_file_name)
        
        print(f"\n{'='*60}")
        print(f"🔄 正在处理: {file_name}")
        print(f"📝 日志输出: {log_file_name}")
        print(f"{'='*60}")
        
        result = process_single_file(json_file, log_file_path)
        if result:
            all_results.append(result)
    
    # 输出汇总统计
    print(f"\n{'='*80}")
    print("📊 所有文件汇总统计")
    print(f"{'='*80}")
    
    if all_results:
        total_files = len(all_results)
        total_items = sum(r['total_items'] for r in all_results)
        total_correct = sum(r['correct_count'] for r in all_results)
        total_partial = sum(r['partial_count'] for r in all_results)
        total_error = sum(r['error_count'] for r in all_results)
        overall_accuracy = (total_correct + total_partial*0.2)/total_items*100 if total_items > 0 else 0
        
        print(f"📁 处理文件数: {total_files}")
        print(f"📊 总测试项目: {total_items}")
        print(f"✅ 完全正确: {total_correct} ({total_correct/total_items*100:.1f}%)")
        print(f"🔶 格式正确但内容不一致: {total_partial} ({total_partial/total_items*100:.1f}%)")
        print(f"❌ 格式错误: {total_error} ({total_error/total_items*100:.1f}%)")
        print(f"🎯 总体准确率: {overall_accuracy:.1f}%")
        
        print(f"\n📋 各文件详细结果:")
        print(f"{'文件名':<50} {'项目数':<8} {'正确':<6} {'部分':<6} {'错误':<6} {'准确率':<8}")
        print("-" * 90)
        for result in all_results:
            print(f"{result['file_name']:<50} {result['total_items']:<8} {result['correct_count']:<6} {result['partial_count']:<6} {result['error_count']:<6} {result['accuracy']:<8.1f}%")
    
    print(f"\n✅ 所有文件处理完成！日志文件保存在: {log_directory}")
