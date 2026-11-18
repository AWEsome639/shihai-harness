# PLE 交付格式普查（2026-11-17 21:29）

## checks 题（12 道）—— 交付形态
```
submission.zip                                    9 道
  cooling-water-flush-plan ｜ data-hall-device-takeoff ｜ division-26-specifications
  frame-extension-method ｜ heat-rejection-life-cycle-cost ｜ mv-conveyance-study
  roof-deck-design ｜ seismic-diaphragm-shear ｜ wind-diaphragm-shear
model.ifc + reconciliation.json + solution.py     2 道
  data-hall-mep-routing ｜ data-hall-mep-routing-offset
（正则没抓到路径，需人工看）                        1 道
  data-center-site-model
```
## rubric 题（14 道）
```
正则未抓到 output/ 路径 —— 多为 .docx/.xlsx，但写法各异
⇒ 需要单独看每篇 instruction 的「Expected output」段
```

## ⇒ 结论
- **9/12 checks 题 = 75% 要交 zip；占全部 29 题的 31%**
- **而 harness 提示词此前**一次都没提过 zip**（`grep -niE "zip|archive|bundle"` = 0）**
- **⇒ 这 9 道若因「只写散文件、不建 zip」而全 0，一句话就可能把它们全部翻过来**
- 已在 `harness.py` 加对应段落（commit 964d5c5），并补了一道要 zip 的题进仓库（commit 7b56c32）用于验证

## wind-diaphragm-shear 的 zip 里必须有 4 个文件（21:30 读到的）
```
design_basis.md          ← markdown，容易
lateral_schedule.xlsx    ← 需要 openpyxl/xlsxwriter
calculations.pdf         ← **需要能写 PDF**（reportlab / fpdf / wkhtmltopdf 之类）
constraint_register.xlsx ← 同 xlsx
```
- **⇒ 这意味着即使模型知道要建 zip，还有第二道坎：能不能**生成 PDF 和 xlsx**
- **⇒ harness 的写作稿说「G3 没有 .docx/.pdf 读取器」——读取和**写入**是两件事，写入需要库**
- **⇒ 今晚要看 verifier 的报错是哪一层：**
```
找不到 submission.zip                ⇒ 第一道坎（zip 提示词没生效）
zip 在但成员缺 PDF/xlsx              ⇒ 第二道坎（生成能力不足）
成员齐但 verifier 判分低              ⇒ 内容质量（那是第三层，也是最后才该谈的）
```
- **另：题目原文自己写着「a run that exhausts its step or token budget with no file scores 0.
  An imperfect early submission earns partial credit」⇒ 官方明说**早交不完美的也有部分分**，与今天早上那条「先落地产物」完全一致**
