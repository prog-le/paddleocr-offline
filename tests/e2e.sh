#!/usr/bin/env bash
# =============================================================================
# 端到端集成测试（MOCK 推理引擎，不需要模型/PaddleX）
#
# 覆盖：上传（图片 + 多页 PDF）→ 队列/进度 → 结果接口 → 四种导出 → 后台登录与状态
#
# 用法：./tests/e2e.sh
# =============================================================================
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../scripts/env.sh"

PY="${VENV_PY:-}"
if [ -z "$PY" ] && [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then PY="$PROJECT_ROOT/.venv/bin/python"; fi
[ -n "$PY" ] || PY="$(command -v python3)"

PORT="${E2E_PORT:-8123}"
BASE="http://127.0.0.1:$PORT"
TMP="$(mktemp -d)"
COOKIE="$TMP/cookies.txt"      # 管理员会话
CJAR="$TMP/client1.txt"        # 浏览器会话 A（前台匿名会话）
CJAR2="$TMP/client2.txt"       # 浏览器会话 B（模拟另一个浏览器/另一个人）
PASS=0; FAIL=0

say()  { printf '\n=== %s ===\n' "$*"; }
pass() { PASS=$((PASS+1)); printf '  \033[32m[通过]\033[0m %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31m[失败]\033[0m %s\n' "$*"; }

cleanup() {
  [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null
  wait "${SERVER_PID:-}" 2>/dev/null
  echo
  echo "临时目录：$TMP"
}
trap cleanup EXIT

say "准备测试素材"
"$PY" "$PROJECT_ROOT/tests/gen_assets.py" "$TMP" || { echo "生成素材失败"; exit 1; }
ls -l "$TMP" | sed -n '2,5p'

say "启动服务（MOCK 模式，端口 ${PORT}）"
cd "$PROJECT_ROOT"
PADDLEX_OCR_MOCK=1 PADDLEX_OCR_PORT="$PORT" PADDLEX_OCR_DATA_DIR="$TMP/data" \
PADDLEX_OCR_LOG_DIR="$TMP/logs" PADDLEX_OCR_ADMIN_PASSWORD="test-pass-123" \
  "$PY" -m uvicorn backend.main:app --host 127.0.0.1 --port "$PORT" --workers 1 --no-access-log \
  > "$TMP/server.log" 2>&1 &
SERVER_PID=$!

for i in $(seq 1 40); do
  if curl -fsS --max-time 2 "$BASE/api/meta" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
if curl -fsS --max-time 2 "$BASE/api/meta" > "$TMP/meta.json"; then
  pass "服务已就绪：$(head -c 120 "$TMP/meta.json")"
else
  bad "服务启动失败"; tail -30 "$TMP/server.log"; exit 1
fi

say "上传：图片 + 多页 PDF（批量）"
curl -sS -b "$CJAR" -c "$CJAR" -X POST "$BASE/api/tasks" \
  -F "files=@$TMP/sample.png" -F "files=@$TMP/multi.pdf" \
  -F 'params={"use_table_recognition":true,"text_det_box_thresh":0.55}' \
  -o "$TMP/tasks.json" -w '%{http_code}' > "$TMP/code.txt"
CODE="$(cat "$TMP/code.txt")"
if [ "$CODE" = "200" ]; then
  N="$("$PY" -c "import json;print(len(json.load(open('$TMP/tasks.json'))['tasks']))")"
  pass "上传成功，创建 $N 个任务"
else
  bad "上传失败 HTTP ${CODE}：$(cat "$TMP/tasks.json")"; exit 1
fi

read -r T1 T2 < <("$PY" -c "
import json
d=json.load(open('$TMP/tasks.json'))
print(' '.join(t['id'] for t in d['tasks']))
")
echo "  task1=$T1  task2=$T2"

say "轮询任务直到完成"
for i in $(seq 1 60); do
  curl -sS -b "$CJAR" -c "$CJAR" "$BASE/api/tasks?limit=20" -o "$TMP/list.json"
  STATUS="$("$PY" -c "
import json
d=json.load(open('$TMP/list.json'))
st=[t['status'] for t in d['items']]
print('done' if all(s in ('success','failed','cancelled') for s in st) else 'waiting')
print(','.join(st))
")"
  echo "$STATUS" | head -1 | grep -q done && break
  sleep 0.5
done
"$PY" - "$TMP/list.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
for t in d["items"]:
    print(f"  {t['id']}  {t['filename']:<16} {t['status']:<8} progress={t['progress']} pages={t['pages_done']}/{t['pages_total']}")
    if t["status"] != "success":
        print("     error:", t.get("error"))
PY2
SUCC="$("$PY" -c "
import json;d=json.load(open('$TMP/list.json'));print(sum(1 for t in d['items'] if t['status']=='success'))")"
if [ "$SUCC" = "2" ]; then pass "两个任务均识别成功"; else bad "成功任务数 ${SUCC}（期望 2）"; fi

say "多页 PDF 分页结果检查"
curl -sS -b "$CJAR" -c "$CJAR" "$BASE/api/tasks/$T2/result" -o "$TMP/result2.json"
"$PY" - "$TMP/result2.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"  pages={len(d['pages'])} stats={d['stats']}")
assert len(d["pages"]) == 2, "多页 PDF 页数不正确"
assert d["stats"]["tables"] >= 2, "未识别到表格"
p0 = d["pages"][0]
assert p0["blocks"], "页面无 block"
tbl = [b for b in p0["blocks"] if b["type"] == "table"][0]["table"]
print(f"  首表 {tbl['n_rows']}x{tbl['n_cols']}，合并单元格 {sum(1 for c in tbl['cells'] if c['rowspan']>1 or c['colspan']>1)} 个")
assert any(c["rowspan"] > 1 or c["colspan"] > 1 for c in tbl["cells"]), "合并信息丢失"
print("  [通过] 分页与表格结构正确")
PY2
[ $? -eq 0 ] && pass "结果结构校验通过" || bad "结果结构校验失败"

say "一键复制文本接口"
curl -sS -b "$CJAR" -c "$CJAR" "$BASE/api/tasks/$T1/text" -o "$TMP/copy.txt"
if [ -s "$TMP/copy.txt" ]; then pass "复制文本 $(wc -c < "$TMP/copy.txt") 字节"; else bad "复制文本为空"; fi

say "四种格式导出"
for fmt in docx xlsx txt md; do
  CODE="$(curl -sS -b "$CJAR" -c "$CJAR" -o "$TMP/out.$fmt" -w '%{http_code}' "$BASE/api/tasks/$T1/export?fmt=$fmt")"
  if [ "$CODE" = "200" ] && [ -s "$TMP/out.$fmt" ]; then
    pass "导出 $fmt 成功（$(wc -c < "$TMP/out.$fmt") 字节）"
  else
    bad "导出 $fmt 失败 HTTP $CODE"
  fi
done
"$PY" "$PROJECT_ROOT/tests/verify_exports.py" "$TMP" out || bad "导出内容校验失败"

say "后台管理：登录 + 概览 + 任务列表 + 日志"
CODE="$(curl -sS -c "$COOKIE" -o "$TMP/login.json" -w '%{http_code}' -X POST "$BASE/api/admin/login" \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"test-pass-123"}')"
[ "$CODE" = "200" ] && pass "登录成功" || bad "登录失败 HTTP $CODE $(cat "$TMP/login.json")"

CODE="$(curl -sS -b "$COOKIE" -o "$TMP/overview.json" -w '%{http_code}' "$BASE/api/admin/overview")"
if [ "$CODE" = "200" ]; then
  "$PY" - "$TMP/overview.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"  服务: {d['service']['pipeline']} mock={d['service']['mock']} uptime={d['service']['uptime_sec']}s")
print(f"  统计: {d['tasks']}")
print(f"  系统: {d['system'].get('arch')} python={d['system'].get('python')} mem={d['system'].get('mem_used_mb')}MB")
assert d["tasks"]["success"] >= 2, d["tasks"]
PY2
  pass "概览接口正常（统计/系统信息齐全）"
else
  bad "概览接口失败 HTTP $CODE"
fi

CODE="$(curl -sS -b "$COOKIE" -o "$TMP/alog.txt" -w '%{http_code}' "$BASE/api/admin/tasks/$T1/log?lines=50")"
[ "$CODE" = "200" ] && pass "单任务日志可读（$(wc -c < "$TMP/alog.txt") 字节）" || bad "任务日志读取失败"

CODE="$(curl -sS -b "$COOKIE" -o "$TMP/syslog.json" -w '%{http_code}' "$BASE/api/admin/logs?file=app.log&lines=30")"
[ "$CODE" = "200" ] && pass "服务日志可读" || bad "服务日志读取失败"

CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/admin/overview")"
[ "$CODE" = "401" ] && pass "未登录访问后台被拒绝（401）" || bad "未登录竟可访问后台（HTTP ${CODE}）"

say "服务控制：暂停接单 → 恢复"
curl -sS -b "$COOKIE" -X POST "$BASE/api/admin/service/pause" -o /dev/null
CODE="$(curl -sS -b "$CJAR" -c "$CJAR" -o /dev/null -w '%{http_code}' -X POST "$BASE/api/tasks" -F "files=@$TMP/sample.png" -F 'params={}')"
[ "$CODE" != "200" ] && pass "暂停后拒绝新任务（HTTP ${CODE}）" || bad "暂停后仍接受任务"
curl -sS -b "$COOKIE" -X POST "$BASE/api/admin/service/resume" -o /dev/null
CODE="$(curl -sS -b "$CJAR" -c "$CJAR" -o /dev/null -w '%{http_code}' -X POST "$BASE/api/tasks" -F "files=@$TMP/sample.png" -F 'params={}')"
[ "$CODE" = "200" ] && pass "恢复后可提交任务" || bad "恢复后仍拒绝（HTTP ${CODE}）"

say "静态页面与静态资源"
for path in "/" "/admin" "/assets/app.css" "/assets/app.js" "/assets/admin.js" "/assets/favicon.svg"; do
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE$path")"
  [ "$CODE" = "200" ] && pass "GET $path -> 200" || bad "GET $path -> $CODE"
done
curl -sS "$BASE/" | grep -q "PaddleX OCR" && pass "首页包含标题" || bad "首页内容异常"
curl -sS "$BASE/assets/app.js" | grep -q "function buildTable" && pass "前端脚本可访问" || bad "前端脚本异常"

say "API 文档（/api/docs 必须离线可用）"
CODE="$(curl -sS -o "$TMP/docs.html" -w '%{http_code}' "$BASE/api/docs")"
[ "$CODE" = "200" ] && pass "GET /api/docs -> 200" || bad "GET /api/docs -> $CODE"
# 页面里不能出现 CDN 地址（离线环境会 404，页面一片空白）
if grep -qE "cdn\.jsdelivr|unpkg\.com|cdnjs" "$TMP/docs.html"; then
  bad "文档页仍引用 CDN 资源"
else
  pass "文档页不引用任何 CDN"
fi
grep -q "/assets/swagger/swagger-ui-bundle.js" "$TMP/docs.html" \
  && pass "文档页引用本地 swagger-ui-bundle.js" || bad "文档页未引用本地 JS"
grep -q "/assets/swagger/swagger-ui.css" "$TMP/docs.html" \
  && pass "文档页引用本地 swagger-ui.css" || bad "文档页未引用本地 CSS"
# 两个静态资源必须能真正取到（Content-Type 与体积都要对）
JS_SIZE="$(curl -sS -o /dev/null -w '%{size_download}' "$BASE/assets/swagger/swagger-ui-bundle.js")"
[ "${JS_SIZE:-0}" -gt 500000 ] && pass "swagger-ui-bundle.js 可下载（${JS_SIZE} B）" || bad "swagger-ui-bundle.js 异常（${JS_SIZE} B）"
CSS_SIZE="$(curl -sS -o /dev/null -w '%{size_download}' "$BASE/assets/swagger/swagger-ui.css")"
[ "${CSS_SIZE:-0}" -gt 50000 ] && pass "swagger-ui.css 可下载（${CSS_SIZE} B）" || bad "swagger-ui.css 异常（${CSS_SIZE} B）"
curl -sS "$BASE/api/openapi.json" | grep -q '"openapi"' && pass "OpenAPI JSON 可访问" || bad "OpenAPI JSON 异常"

say "在线配置：修改上传限额并验证生效"
CODE="$(curl -sS -b "$COOKIE" -o "$TMP/cfg.json" -w '%{http_code}' -X PATCH "$BASE/api/admin/config" \
  -H 'Content-Type: application/json' \
  -d '{"limits":{"max_upload_mb":1},"ocr":{"defaults":{"text_det_box_thresh":0.45}}}')"
if [ "$CODE" = "200" ]; then
  "$PY" - "$TMP/cfg.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
ap = d.get("applied", {})
assert ap.get("limits", {}).get("max_upload_mb") == 1, ap
assert ap.get("ocr", {}).get("defaults", {}).get("text_det_box_thresh") == 0.45, ap
print("  applied:", ap)
PY2
  [ $? -eq 0 ] && pass "嵌套配置项已生效（含阈值）" || bad "配置未按预期生效"
else
  bad "配置接口失败 HTTP $CODE"
fi
curl -sS "$BASE/api/meta" -o "$TMP/meta2.json"
"$PY" -c "
import json;d=json.load(open('$TMP/meta2.json'))
assert d['limits']['max_upload_mb']==1, d['limits']
assert abs(d['ocr']['defaults']['text_det_box_thresh']-0.45)<1e-6
print('  meta 已同步')
" && pass "公开配置同步更新" || bad "公开配置未同步"

"$PY" -c "
from PIL import Image
Image.new('RGB',(64,64),'white').save('$TMP/big.png')
data=open('$TMP/big.png','rb').read()
open('$TMP/big.png','wb').write(data + b'\0'*(2*1024*1024))
"
CODE="$(curl -sS -b "$CJAR" -c "$CJAR" -o "$TMP/big.json" -w '%{http_code}' -X POST "$BASE/api/tasks" -F "files=@$TMP/big.png" -F 'params={}')"
[ "$CODE" = "413" ] && pass "超限文件被拒绝（413）" || bad "超限文件未被拒绝（HTTP ${CODE}）$(head -c 80 "$TMP/big.json")"

curl -sS -b "$COOKIE" -X PATCH "$BASE/api/admin/config" -H 'Content-Type: application/json' \
  -d '{"limits.max_upload_mb":200}' -o /dev/null
curl -sS "$BASE/api/meta" | grep -q '"max_upload_mb": *200' && pass "限额已恢复为 200MB" || bad "限额恢复失败"

say "删除任务"
CODE="$(curl -sS -b "$CJAR" -c "$CJAR" -o /dev/null -w '%{http_code}' -X DELETE "$BASE/api/tasks/$T1")"
[ "$CODE" = "200" ] && pass "删除任务成功" || bad "删除任务失败 HTTP $CODE"
CODE="$(curl -sS -b "$CJAR" -c "$CJAR" -o /dev/null -w '%{http_code}' "$BASE/api/tasks/$T1")"
[ "$CODE" = "404" ] && pass "删除后查询返回 404" || bad "删除后仍可查询（HTTP ${CODE}）"

say "多产线：可选列表 + 任务级产线选择"
curl -sS "$BASE/api/meta" -o "$TMP/meta3.json"
"$PY" -c "
import json
d=json.load(open('$TMP/meta3.json'))
ids=[p['id'] for p in d['pipelines']]
kinds={p['id']: p['kind'] for p in d['pipelines']}
expect=['OCR','PP-StructureV3','PP-StructureV3-full','table_recognition_v2']
assert ids==expect, ('产线顺序/条目不符合预期', ids, expect)
assert kinds['OCR']=='ocr', kinds
assert kinds['table_recognition_v2']=='table', kinds
assert kinds['PP-StructureV3']=='structure', kinds
assert d['pipeline'] in ids, (d['pipeline'], ids)
print('  产线顺序:', ids)
print('  类型:', kinds)
print('  默认产线:', d['pipeline'])
" && pass "产线列表四条且顺序为 纯文字→精简→完整→通用表格识别" || bad "产线列表/顺序异常"

# 指定 OCR 产线提交
curl -sS -b "$CJAR" -c "$CJAR" -X POST "$BASE/api/tasks" \
  -F "files=@$TMP/sample.png" -F 'params={}' -F 'pipeline=OCR' \
  -o "$TMP/p_ocr.json" -w '%{http_code}' > "$TMP/p_ocr.code"
"$PY" -c "
import json
d=json.load(open('$TMP/p_ocr.json'))
t=d['tasks'][0]
assert t['pipeline']=='OCR', t['pipeline']
assert 'OCR' in (t.get('pipeline_label') or ''), t.get('pipeline_label')
print('  任务产线:', t['pipeline'], '/', t.get('pipeline_label'))
" && pass "按产线提交（OCR）成功且任务记录产线" || bad "产线参数未生效"

# 指定结构产线提交
curl -sS -b "$CJAR" -c "$CJAR" -X POST "$BASE/api/tasks" \
  -F "files=@$TMP/sample.png" -F 'params={}' -F 'pipeline=PP-StructureV3' \
  -o "$TMP/p_str.json" -w '%{http_code}' > /dev/null
"$PY" -c "
import json
d=json.load(open('$TMP/p_str.json'))
t=d['tasks'][0]
assert t['pipeline']=='PP-StructureV3', t['pipeline']
print('  任务产线:', t['pipeline'], '/', t.get('pipeline_label'))
" && pass "按产线提交（通用版面解析 v3）成功" || bad "结构产线提交失败"

# 指定通用表格识别产线提交
curl -sS -b "$CJAR" -c "$CJAR" -X POST "$BASE/api/tasks" \
  -F "files=@$TMP/sample.png" -F 'params={"use_ocr_results_with_table_cells":false}' \
  -F 'pipeline=table_recognition_v2' \
  -o "$TMP/p_tbl.json" -w '%{http_code}' > /dev/null
"$PY" -c "
import json
d=json.load(open('$TMP/p_tbl.json'))
t=d['tasks'][0]
assert t['pipeline']=='table_recognition_v2', t['pipeline']
assert t.get('pipeline_label')=='通用表格识别', t.get('pipeline_label')
print('  任务产线:', t['pipeline'], '/', t.get('pipeline_label'))
" && pass "按产线提交（通用表格识别）成功" || bad "通用表格识别产线提交失败"

say "运行设备：探测结果、CPU/GPU 切换与显存/批处理配置"
"$PY" -c "
import json
d=json.load(open('$TMP/meta3.json'))
dev=d.get('device') or {}
act=dev.get('active') or {}
assert dev, 'meta.device 缺失'
assert act.get('device','').startswith(('cpu','gpu')), act
assert 'apple_silicon' in (dev.get('platform') or {}), dev.get('platform')
assert 'cuda' in (dev.get('capabilities') or {}), dev.get('capabilities')
assert int((dev.get('batching') or {}).get('text_rec_batch_size') or 0) >= 1, dev.get('batching')
assert dev.get('ocr_tier') in ('tiny','small','medium'), dev.get('ocr_tier')
print('  设备:', act.get('device'), '/', act.get('name'), '| CUDA 版:', (dev.get('capabilities') or {}).get('cuda'))
print('  可用 GPU:', [g['name'] for g in (dev.get('gpus') or [])] or '无')
print('  batching:', dev.get('batching'), '| tier:', dev.get('ocr_tier'))
" && pass "meta 暴露设备/显存/批处理信息" || bad "设备信息缺失"

# 非法设备配置必须 400（不能把不可用配置写进覆盖文件）
CODE="$(curl -sS -b "$COOKIE" -o "$TMP/dev_bad.json" -w '%{http_code}' -X PATCH "$BASE/api/admin/config" \
  -H 'Content-Type: application/json' -d '{"ocr":{"device":"tpu:9"}}')"
[ "$CODE" = "400" ] && pass "非法设备配置被拒绝（400）" || bad "非法设备未被拒绝（HTTP $CODE）"

# 改批大小：应返回 requires_reload=true，并真的落到配置里
CODE="$(curl -sS -b "$COOKIE" -o "$TMP/batch.json" -w '%{http_code}' -X PATCH "$BASE/api/admin/config" \
  -H 'Content-Type: application/json' -d '{"ocr":{"text_rec_batch_size":12,"ocr_tier":"small"}}')"
if [ "$CODE" = "200" ]; then
  "$PY" -c "
import json
d=json.load(open('$TMP/batch.json'))
ap=d.get('applied',{}).get('ocr',{})
assert ap.get('text_rec_batch_size')==12, ap
assert ap.get('ocr_tier')=='small', ap
assert d.get('requires_reload') is True, d.get('requires_reload')
print('  applied:', ap, '| requires_reload:', d.get('requires_reload'))
" && pass "批处理/档位可在线修改且提示需重载" || bad "批处理配置未按预期生效"
else
  bad "批处理配置接口失败 HTTP $CODE"
fi
# 还原，避免影响后续用例
curl -sS -b "$COOKIE" -X PATCH "$BASE/api/admin/config" -H 'Content-Type: application/json' \
  -d '{"ocr":{"text_rec_batch_size":6,"ocr_tier":"medium"}}' -o /dev/null

# 后台概览应带设备摘要
curl -sS -b "$COOKIE" "$BASE/api/admin/overview" -o "$TMP/ov3.json"
"$PY" -c "
import json
d=json.load(open('$TMP/ov3.json'))
ds=d['service'].get('device_summary') or {}
assert ds.get('active',{}).get('device'), ds
print('  后台设备:', ds['active']['device'], '| 配置:', ds.get('configured'))
" && pass "后台概览含设备摘要" || bad "后台缺少设备摘要"

# 非法产线应 400 并列出可选项
CODE="$(curl -sS -b "$CJAR" -o "$TMP/p_bad.json" -w '%{http_code}' -X POST "$BASE/api/tasks" \
  -F "files=@$TMP/sample.png" -F 'params={}' -F 'pipeline=NoSuchPipeline')"
[ "$CODE" = "400" ] && pass "非法产线被拒绝（400）：$(head -c 80 "$TMP/p_bad.json")" || bad "非法产线未拒绝（HTTP ${CODE}）"

# 后台概览应能看到每条产线状态
curl -sS -b "$COOKIE" "$BASE/api/admin/overview" -o "$TMP/ov2.json"
"$PY" -c "
import json
d=json.load(open('$TMP/ov2.json'))
eng=d['engine']
sub=eng.get('pipelines') or {}
assert sub, 'engine.pipelines 缺失'
print('  产线状态:', {k: v.get('state') for k,v in sub.items()})
print('  默认:', eng.get('default_pipeline'))
" && pass "后台概览含各产线状态" || bad "后台产线状态缺失"

say "会话隔离：另一个浏览器看不到本会话的文件与结果"
# 模拟"新开一个浏览器"：无 Cookie 首次访问，服务端应签发新会话
curl -sS -c "$CJAR2" "$BASE/api/tasks?limit=5" -o "$TMP/b_list.json"
"$PY" -c "
import json;d=json.load(open('$TMP/b_list.json'))
assert d['total']==0, ('会话B 不应看到任务', d['total'])
print('  会话B 可见任务数:', d['total'])
" && pass "新浏览器任务列表为空（看不到他人任务）" || bad "会话隔离失败：新浏览器看到了任务"

# 会话 B 直接猜测/复用 A 的任务 ID：一律 404
for pth in "/api/tasks/$T2" "/api/tasks/$T2/result" "/api/tasks/$T2/text" "/api/tasks/$T2/export?fmt=txt"; do
  CODE="$(curl -sS -b "$CJAR2" -o /dev/null -w '%{http_code}' "$BASE$pth")"
  [ "$CODE" = "404" ] && pass "会话B 访问 A 的 $pth -> 404" || bad "会话B 竟可访问 ${pth}（HTTP ${CODE}）"
done
CODE="$(curl -sS -b "$CJAR2" -o /dev/null -w '%{http_code}' -X POST "$BASE/api/tasks/$T2/cancel")"
[ "$CODE" = "404" ] && pass "会话B 无法取消 A 的任务" || bad "会话B 可取消 A 的任务（HTTP ${CODE}）"
CODE="$(curl -sS -b "$CJAR2" -o /dev/null -w '%{http_code}' -X DELETE "$BASE/api/tasks/$T2")"
[ "$CODE" = "404" ] && pass "会话B 无法删除 A 的任务" || bad "会话B 可删除 A 的任务（HTTP ${CODE}）"

# 会话 A 自己仍可正常访问
CODE="$(curl -sS -b "$CJAR" -o /dev/null -w '%{http_code}' "$BASE/api/tasks/$T2")"
[ "$CODE" = "200" ] && pass "会话A 访问自己的任务正常（200）" || bad "会话A 访问自己的任务失败（HTTP ${CODE}）"

# 伪造/篡改 Cookie 不能拿到任何数据（签名校验）
CODE="$(curl -sS -H 'Cookie: pdx_sid=deadbeefdeadbeef.badsignature' -o "$TMP/forged.json" -w '%{http_code}' "$BASE/api/tasks?limit=5")"
"$PY" -c "
import json;d=json.load(open('$TMP/forged.json'))
assert d['total']==0, d['total']
print('  篡改 Cookie 后可见任务数:', d['total'])
" && pass "篡改 Cookie 被拒绝（重新签发会话，无数据泄露）" || bad "篡改 Cookie 拿到了数据"

# 两个会话的 id 必须不同
A_ID="$(curl -sS -b "$CJAR"  "$BASE/api/meta" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["session"]["id"])')"
B_ID="$(curl -sS -b "$CJAR2" "$BASE/api/meta" | "$PY" -c 'import json,sys;print(json.load(sys.stdin)["session"]["id"])')"
[ -n "$A_ID" ] && [ "$A_ID" != "$B_ID" ] && pass "会话标识隔离（A=$A_ID B=${B_ID}）" || bad "会话标识未隔离（A=$A_ID B=${B_ID}）"

# 管理员后台仍应看到全部任务，并显示会话列
curl -sS -b "$COOKIE" "$BASE/api/admin/tasks?limit=50" -o "$TMP/a_tasks.json"
"$PY" - "$TMP/a_tasks.json" <<'PY2'
import json, sys
d = json.load(open(sys.argv[1]))
items = d["items"]
assert len(items) >= 2, f"管理员应看到全部任务，实际 {len(items)}"
sessions = {i.get("session") for i in items}
assert all(s and s != "-" for s in sessions), sessions
assert "owner" not in items[0], "不应向管理员返回完整 owner"
print(f"  管理员可见任务 {len(items)} 条，会话标识 {sorted(sessions)}")
PY2
[ $? -eq 0 ] && pass "管理员可见全部任务且带会话标识" || bad "管理员视图异常"

say "异常处理：不支持格式 / 伪装文件"
printf 'not an image' > "$TMP/fake.png"
CODE="$(curl -sS -b "$CJAR" -c "$CJAR" -o "$TMP/err1.json" -w '%{http_code}' -X POST "$BASE/api/tasks" -F "files=@$TMP/fake.png" -F 'params={}')"
[ "$CODE" = "400" ] && pass "伪装文件被拒绝（400）：$(head -c 90 "$TMP/err1.json")" || bad "伪装文件未被拒绝（HTTP ${CODE}）"

printf 'x' > "$TMP/bad.txt"
CODE="$(curl -sS -b "$CJAR" -c "$CJAR" -o /dev/null -w '%{http_code}' -X POST "$BASE/api/tasks" -F "files=@$TMP/bad.txt" -F 'params={}')"
[ "$CODE" = "400" ] && pass "不支持扩展名被拒绝（400）" || bad "不支持扩展名未被拒绝（HTTP ${CODE}）"

say "结果"
echo "-----------------------------------------"
echo " 通过 $PASS 项，失败 $FAIL 项"
echo "-----------------------------------------"
[ "$FAIL" -eq 0 ] || exit 1
