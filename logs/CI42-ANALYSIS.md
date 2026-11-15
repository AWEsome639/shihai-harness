# CI42 分析

**证据边界（先说清）**：`ci42.log` 是 CI 包装日志，不是 agent 全程日志——它只保留了 harness 日志的**最后 4000 字符**（第 886 行 `--- harness log ---` 起）和最终回复。本轮（trial `NkYT2SE`）的 `solution.py` 与 `my-harness.txt` 没有落到本机，所以「第一元件坐标」「gates.py 源码」只能由它自己的结论反推，推断处已标注。未读 `tests/evaluator/`。

## 一句话病因

它没把服务入口当成「必须从图上量出来的量」，而是自己定在**东墙 x=35500**（合同值是 `(200, 3360)`）；随后 `gates.py` 又拿同一个自定常量当尺子量自己——**模型与自查器共享同一套臆造常量**，于是 16/16 PASS，官方两道门 0/2。

## 事实（带原文引用）

### 1. 入口坐标是否被读到：没有证据，且它自己承认图纸没读透

- 它报的入口在**东墙**：「Connectivity — tray **16 elements = 1 component**, busway 17 = 1, CHW 31 = 1; each reaches the **east-wall entry at x=35500 mm**」（第 929 行）。
- 官方尺子在西侧：「nothing within 2500mm of the service entry at **(200, 3360)** — the network is not fed」（第 673–674 行）。
- 同一份最终回复自认读图失败：「headless OCR of the cabinet schedule was impossible (no PIL, no tesseract), so that figure is the one leg I cannot claim to have measured against the drawing.」（第 934 行）
- ⇒ 35500 **没有出处**（没写读自哪张图、哪块裁剪），同轮又自认 OCR 失败。最可能是没读、用了臆造值；即便它真读了东墙那个入口，也说明它没核对「tray/busway 该从哪个入口进」。

### 2. 实际第一个元件位置：x≈35500

由它自己的连通性断言反推（第 929 行「reaches the east-wall entry at x=35500 mm」）——到 `(200, 3360)` 的平面距离 ≈ **35.3 m**，是 2500 mm 门槛的 14 倍。tray 16 段、busway 17 段，各自都是 1 个连通分量。`routed_length`：cable_tray 104,900 mm、busway 112,800 mm（第 630–631 行）。**y 坐标保留日志里没有，不编。**

### 3. 自查器的盲区（核心）

它自写 `gates.py` 的实测输出（第 889 行）：

```
wrote output/model.ifc : 128 racks, 20622 products
=== CLASH ===   HARD CLASHES: 0
=== GATES ===
PASS  G1 schema IFC4        PASS  G1 express_rules 0 errors     PASS  G2 hard clashes == 0
PASS  G3a tray coverage == 1.0      PASS  G3a busway coverage==1…
```

三层盲区：

- **只查拓扑，不查「被喂到」**：连通门被降级成「元件数 = 1 个连通分量」（第 929 行）。门问的是「离入口 2500 mm 内有没有元件」，这个数它从头到尾没算。
- **量入口时用错常量**：它拿自己定的 35500 当入口，而不是合同的 `(200, 3360)`——所以自查器永远不会报「没接上」，只会报「接上了」。
- **覆盖率量在自己的行上**：`8 rows × 16 = 128 racks; tray 1.0, busway 1.0`（第 928 行）；官方的行清单是 **6 行**（第 683 行 `"n_rows": 6`），每行 cable_tray 只有 **0.5**（第 749 行）。**自己摆的行上量出来的 1.0，等于没量。**
- 附带：CRAH 只建了 **7** 台（第 930 行），官方 CHW 末端 **22** 个（第 684 行），5 台根本不在模型里（第 676–681 行 `unreached_consumers`）。

### 4. 是否发现 unserved_rows：没有

开篇即「All 16 acceptance criteria pass from a clean build.」（第 911 行），全文没有一行提到未服务行；而官方 A–F 六个行全部未服务（第 641–670 行），`exec_status: ERR_CONNECTIVITY`（第 691 行）。它把唯一真过的一条（clash 0，第 626 行）当成了整体过关。

### 5. 交付文件里 tray/busway 的坐标范围

起点 x≈35500（东墙），沿墙/沿排铺开；tray 16 段共 104.9 m、busway 17 段共 112.8 m。具体 y 范围不可得（`solution.py` 未落本机）。可算的对照：总长 424.0 m 对 `L_opt` 260.6 m（1.63×）——但连通门已是 0 分，效率分不参与。

## 根本原因

不是「没读到入口坐标」一句能概括的，是**三层叠加，最后被自查器闭环锁死**：

1. **图纸基本没读**。容器只有 `numpy / ifcopenshell`（`environment/requirements.txt`），没有 PIL、tesseract。它据此判定「没法 OCR」，转向假设：8 行 × 16、7 台 CRAH、入口在东墙。但**上一轮同一任务（11-15 03:41，trial `Js6ng2t`）的 agent 用 `zlib + numpy` 自己写了 PNG 解码 + ASCII 渲染 + 字形分割**，证明读图可行。这轮是放弃了，不是做不到。
2. **入口被当成「自己选的一个数」**。`instruction.md` 明说「Route three overhead services **from the entries marked on M-101/E-101**」（第 22 行）、「No JSON side-channel, no coordinate dump」（第 15 行）。入口是必须从图里量出来的量，它却报了个无出处的 35500。
3. **自查器做成闭环（真正的病根）**。`gates.py` 与 `solution.py` 共用同一套臆造常量（入口、行数、CRAH 数）。自查器量的是「我的模型和我假设的世界是否自洽」，不是「合同打分的那个量」——所以 16/16 PASS 与 0/2 可以同时成立，而它看不出矛盾。
   ⚠️ **现有 NOTE 在反向鼓励这个**：NOTE 写「The service entry is a stated coordinate…」，但合同里入口只标在图上、没有坐标。模型被要求「给出一个坐标」，就编了一个。这句必须改。

## 下一轮 NOTE 建议（核心产物）

1. 入口坐标必须带出处落地：`solution.py` 顶部写 `ENTRY = (x, y)  # 读自 M-101/E-101 的哪一处`，每次运行第一行打印它；读不出来就写 `ENTRY_UNKNOWN` 并停下——**禁止使用没有出处的数字**。
2. `gates.py` 从 `solution.py` **import** ENTRY（不许各自再定义），每次打印 `dist(ENTRY, 每个服务离它最近的元件)`；**> 2000 mm 即 FAIL**，这个数 ≤ 2000 之前不许写 "all criteria pass"。
3. 连通门按「被入口喂到」判、不按「一个分量」判：打印距 ENTRY 2500 mm 内那个元件的 component id，要求它和各排 header 同属一个分量；**只有 1 个分量不算过**。
4. 行数一律从 A-601 来：打印读到的行数、每行机柜数、每行 y 跨度；总数 ≠ 128 或按**图上**跨度算的覆盖率任一行 < 0.8 就判失败——自己摆的行上量出来的 1.0 不算数。
5. 没有 PIL/tesseract 不等于读不了图：用 `zlib + numpy` 解 1600×1100 PNG（8-bit RGB），把入口符号那一小块打成 ASCII 掩码，对照边框尺寸核坐标；**不许拿「OCR 不可能」当假设的许可证**。
6. 报告里每个门量都标来源：写「读自哪张图的哪块裁剪」或 `assumed`；出现 `assumed` 的门就是未验证，直接写「该门未验证」，**不许算进 16/16**。
