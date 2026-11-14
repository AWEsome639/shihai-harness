# Verifier

Harbor grades this task in a verifier container of its own: the task's image with `tests/` copied in (the evaluation
server builds it so), after the agent finishes. The agent's files, its final message and, for a world task, the apps'
storage arrive there as artifacts.

`test.sh` runs the task's collect steps (vendor renderings and programmatic scores included), then `verify.py`, which
stages what the judge reads and runs `grader/grade.py`: the task's judge, with the rubric in `grader/criteria.json`,
calling any OpenAI-compatible endpoint given as `JUDGE_BASE_URL`, `JUDGE_API_KEY` and `JUDGE_MODEL` (no value of them
is in this folder). `grade.json` records the reward; a grading that fails leaves `GRADER_FAILED.txt` and no reward,
so the trial is not measured.
