# dev 代理的局限（写给以后的自己，也写给评审可能的提问）

> 一句话：**这个 dev 分数不是官方分数**。它有三个已知偏差，每一个都能把真分数压低。

## 1. 资源被裁剪（今天才发现，影响最大）
```
官方 dev 题声明：cpus = 4、memory_mb = 8192~12288（29 道里有 **17 道**）
GitHub runner  ：2 核
⇒ dockerd 直接拒绝 --cpus 4，容器**根本不启动**，分数恒 0
   原话：range of CPUs is from 0.01 to 2.00, as there are only 2 CPUs available
⇒ 修法（commit ed1c6a2）：跑前把 task.toml 的 cpus 改 2、memory 改 4096，并打印改过的题
⇒ 遗留：2 核 + 4 GB 跑一份 4 核 8 GB 的任务，**超时会更多**；这 17 道的 dev 分数天然偏低
```

## 2. 模型走本地中继
```
官方要 claude-opus-5-5；dev 跑时把 submission.yaml 的 model_api sed 成 google-cloud/gpt-oss-120b
⇒ 真正打到的是本机 relay（AMD Token Factory 的 DeepSeek-V4-Flash）
⇒ 「模型名」与「实际模型」不一致；分数只对实际模型有效
```

## 3. 单次方差极大
```
同一道题、同一模型：fire-test-validation-memo 跑出过 1.0 和 0.0
⇒ 任何单次 dev 分数只能说明「跑通了」，不能说明「水平」
⇒ 结论级判断一律 trials ≥ 3
```

## 4. 载体差异
```
dev 只跑 12 道（cpus=2）+ 修完资源后的 17 道；正式评测跑全部 29 道
⇒ dev 与真榜之间不存在一一对应，只能看「方向」
```

## ⇒ 所以我们在 dev 上能相信什么
- ✅ 「容器起了没有」「文件写出来没有」「格式对不对」——这些是确定性的，可信
- ✅ 「改了提示词后同一道题从 0 变成 >0」——方向可信（只要同时报三次）
- ⚠️ 「总分 0.333」「几何类 0」——**不可信**，一半是资源裁剪造成的
