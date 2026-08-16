.PHONY: bootstrap build-llama download-models doctor test dry-run run clean-runs

bootstrap:
	./scripts/bootstrap.sh

build-llama:
	./scripts/build_llama_cpp.sh

download-models:
	.venv-blog-agent/bin/python scripts/download_models.py

doctor:
	./scripts/doctor.sh

test:
	.venv-blog-agent/bin/python -m pytest -q

dry-run:
	.venv-blog-agent/bin/python -m app.cli --image inputs/example.jpg --topic "사진으로 기록하는 하루" --dry-run

run:
	.venv-blog-agent/bin/python -m app.cli --image inputs/example.jpg --topic "사진으로 기록하는 하루"

clean-runs:
	find runs -mindepth 1 ! -name '.gitkeep' -exec rm -rf {} +
