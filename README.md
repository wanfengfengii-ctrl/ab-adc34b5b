# 立方晶胞粉末衍射指数化服务（纯后端）

面向同步辐射材料实验室复核粉末衍射数据的**纯后端**晶胞指数化服务。
Python 3.13 + FastAPI，无任何前端。全程使用 `fractions.Fraction` 精确有理运算，
**禁止浮点比较、禁止贪心配峰**；求解为带精确松弛剪枝的穷举搜索，证书复核时
**用真实求解器重算**，绝不信任证书内嵌答案。

## 问题定义

工程师向版本化 JSON 接口提交：

- `peaks`：**4–32 个严格递增的正有理数**峰位；
- `tolerance`：绝对容差（非负有理数）；
- `max_impurities`：杂质峰额度，`0–2`。

服务把保留峰按峰序映射到不同的可表示值

```
N = h² + k² + l²,   0 ≤ h ≤ k ≤ l ≤ 12
```

并求未知正比例因子 `c > 0`，使每个保留峰满足

```
|p_i − c · N_j| ≤ tolerance
```

指数一律规范为 `h ≤ k ≤ l`；每个可表示值只保留一个规范见证 `(h,k,l)`。
同一物理映射点（同一个 `N`）不重复。

### 优化准则（严格词序）

1. 杂质峰数量；
2. 首末映射之间遗漏的可表示值数量；
3. 最大绝对残差；
4. 残差总和。

四项全部相同但**峰→N 映射不同**时，结果标记为 `ambiguous`，返回两组规范见证；
否则返回唯一解。固定映射下第 3、4 项在可行 `c` 区间上通过精确的分段线性凸优化
（折点均为有理数）最小化。

## 运行

### Docker Compose（推荐）

```bash
# 启动单个 API，宿主端口可用 INDEXING_PORT 配置
INDEXING_PORT=8080 docker compose up --build

# 一次性验收入口（真实求解 + 真实证书复核），全绿退出码为 0
docker compose --profile acceptance run --rm acceptance
```

### 本地

```bash
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080

# 一次性验收（CLI 等价入口）
python -m app.acceptance        # 全绿退出码 0

# 测试（含与独立暴力参考实现的穷举等价对照）
pip install -r requirements-dev.txt
pytest -q
```

环境变量：

- `CERT_SECRET`：结果证书 HMAC 签名密钥，生产必须覆盖默认开发值；
- `INDEXING_PORT`：compose 映射到容器 `8080` 的宿主机端口（默认 `8080`）。

## 接口

### `GET /healthz`

```json
{"status": "ok"}
```

### `POST /api/v1/index`

有理数可用 JSON 整数、十进制/分数字符串（`"1.5"`、`"3/7"`）或
`{"num": p, "den": q}` 表示；**JSON 浮点字面量一律拒绝**。

```json
{
  "peaks": ["3/2", "3", "9/2", "6"],
  "tolerance": "0",
  "max_impurities": 0
}
```

`200` 响应（唯一解）：

```json
{
  "status": "unique",
  "peaks": ["3/2", "3", "9/2", "6"],
  "tolerance": "0",
  "max_impurities": 0,
  "solutions": [{
    "scale_factor": "3/2",
    "impurity_indices": [],
    "skipped_representable": 0,
    "max_abs_residual": "0",
    "sum_abs_residual": "0",
    "mappings": [
      {"peak_index": 0, "n": 1, "hkl": [0, 0, 1], "residual": "0", "within_tolerance": true}
    ]
  }],
  "certificate": "<body>.<hmac>"
}
```

歧义时 `status` 为 `ambiguous`，`solutions` 恰含两组规范见证。

### `POST /api/v1/verify`

```json
{"certificate": "<body>.<hmac>"}
```

先校验 HMAC 签名，再用**真实求解器**对证书内嵌输入重算，并把重算结果与内嵌答案
逐一比较；任一处被改动都会被拒绝。

### `POST /api/v1/acceptance`

一次性验收自检，返回 `all_passed / passed / total / checks[]`。

## 稳定机器码（错误响应不泄漏半成品结果）

| HTTP | code | 含义 |
| --- | --- | --- |
| 422 | `INVALID_REQUEST_BODY` | 请求体结构错误/缺字段 |
| 422 | `INVALID_RATIONAL` | 有理数无法解析（含浮点字面量） |
| 422 | `PEAK_COUNT_OUT_OF_RANGE` | 峰数不在 4–32 |
| 422 | `PEAKS_NOT_STRICTLY_INCREASING` | 峰位非严格递增 |
| 422 | `NON_POSITIVE_PEAK` | 存在非正峰位 |
| 422 | `INVALID_TOLERANCE` | 容差为负或非法 |
| 422 | `IMPURITY_BUDGET_OUT_OF_RANGE` | 杂质额度不在 0–2 |
| 422 | `NO_SOLUTION` | 容差/额度内无可行映射 |
| 422 | `ENGINE_LIMIT_REACHED` | 触发组合安全上限（非半成品） |
| 400 | `CERTIFICATE_MALFORMED` | 证书结构损坏 |
| 400 | `CERTIFICATE_SIGNATURE_INVALID` | 签名不匹配 |
| 409 | `CERTIFICATE_RECOMPUTATION_MISMATCH` | 签名虽有效，但内嵌答案与真实重算不一致（被篡改/过期） |

错误响应统一形如 `{"error": {"code", "message", "details"}}`，不含任何求解中间结果。

## 算法与正确性

- `app/representable.py`：构建并缓存 `0–12` 规范指数的全部不同可表示值。
- `app/solver.py`：区间传播穷举 DFS。部分映射维护可行 `c` 区间的交；每个节点用
  精确的前向松弛给出“至少还需多少杂质/跳过多少值”的下界，用整数 `ceil/floor`
  + 二分把下一峰夹到精确可表示值窗口，并对同 `(杂质,跳过)` 类别做**精确**残差
  分支定界（部分映射折点枚举与加权中位，均为有理数）。搜索前用真实可行的贪心潜水
  建立剪枝上界，但穷举仍为唯一权威，种子不可能改变答案。
- `app/certificate.py`：规范 JSON + HMAC-SHA256。
- `tests/test_solver.py`：独立的无最优剪枝暴力参考实现，与生产求解器在精确、紧容差、
  宽容差受限域等多组确定性语料上逐一对照（状态、最优四项、歧义见证集合完全一致），
  并对全部返回解做可行性不变式校验与 32 峰性能校验。
