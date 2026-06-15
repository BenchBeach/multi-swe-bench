from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.checkout_bug_versions import (
    DEFAULT_DATASET_FILES,
    DEFAULT_OUTPUT_DIR as DEFAULT_CHECKOUT_DIR,
    expand_dataset_files,
    load_instances,
)


DEFAULT_MAJOR_HOME = Path("data/major/major")
DEFAULT_BUILD_ROOT = Path("data/major_work/build")
DEFAULT_OUTPUT_ROOT = Path("data/mutation_work")


@dataclass(frozen=True)
class MajorTarget:
    org: str
    repo: str
    number: int
    instance_id: str
    f2p_tests: tuple[str, ...]
    mutation_scopes: tuple[str, ...]

    @property
    def mswe_image(self) -> str:
        return f"mswebench/{self.org}_m_{self.repo}:pr-{self.number}".lower()

    @property
    def major_image(self) -> str:
        return f"majorbench/{self.org}_m_{self.repo}:pr-{self.number}".lower()


def run_cmd(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout)
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
        )
    return result.stdout


def docker_image_exists(image: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def select_one_target(args: argparse.Namespace) -> MajorTarget:
    specifics = set(args.specifics or [])
    if len(specifics) != 1:
        raise ValueError("Please pass exactly one instance with --specifics.")

    dataset_files = expand_dataset_files(args.dataset_files)
    instances = load_instances(dataset_files, specifics)
    if len(instances) != 1:
        ids = ", ".join(instance.instance_id for instance in instances)
        raise ValueError(
            f"Expected exactly one selected instance, got {len(instances)}: {ids}"
        )

    instance = instances[0]
    raw = load_instance_raw(dataset_files, instance.instance_id)
    f2p_tests = tuple(raw.get("f2p_tests", {}).keys())
    mutation_scopes = tuple(extract_java_scopes(raw.get("fix_patch", "")))
    return MajorTarget(
        org=instance.org,
        repo=instance.repo,
        number=instance.number,
        instance_id=instance.instance_id,
        f2p_tests=f2p_tests,
        mutation_scopes=mutation_scopes,
    )


def load_instance_raw(dataset_files: list[Path], instance_id: str) -> dict:
    for dataset_file in dataset_files:
        with dataset_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                data = json.loads(line)
                current_id = f"{data['org']}__{data['repo']}-{data['number']}"
                if current_id == instance_id:
                    return data
    raise ValueError(f"Instance not found in dataset files: {instance_id}")


def extract_java_scopes(patch: str) -> list[str]:
    scopes: list[str] = []
    seen: set[str] = set()
    marker = "/src/main/java/"
    for line in patch.splitlines():
        if not line.startswith(("diff --git ", "+++ b/", "--- a/")):
            continue
        for token in line.split():
            if marker not in token or not token.endswith(".java"):
                continue
            path = token.split(marker, 1)[1]
            scope = path[:-5].replace("/", ".")
            if scope not in seen:
                scopes.append(scope)
                seen.add(scope)
    return scopes


def ignore_checkout_files(include_git: bool):
    excluded = {
        "target",
        "build",
        ".gradle",
        ".mvn/.gradle",
        ".agents",
        ".codex",
    }
    if not include_git:
        excluded.add(".git")

    def _ignore(_dir: str, names: list[str]) -> set[str]:
        return {name for name in names if name in excluded}

    return _ignore


def copy_tree(source: Path, destination: Path, include_git: bool) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=ignore_checkout_files(include_git))


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def dockerfile(target: MajorTarget, iterative_base_image: str | None = None) -> str:
    if iterative_base_image:
        return f"""FROM {iterative_base_image}

USER root

RUN rm -rf /workspace /home/{target.repo}
WORKDIR /workspace

COPY major /opt/major
COPY major-wrapper /opt/major-wrapper
COPY repo /workspace/repo
COPY scripts /major-scripts

RUN chmod +x /opt/major/bin/* /opt/major-wrapper/* /major-scripts/*.sh && \\
    if [ ! -x /opt/major-jdk/bin/javac ]; then \\
      jdk_home="$(dirname "$(dirname "$(readlink -f "$(command -v javac)")")")"; \\
      ln -sfn "${{jdk_home}}" /opt/major-jdk; \\
    fi && \\
    sed -i 's/^    javac /    \\/opt\\/major-jdk\\/bin\\/javac /' /opt/major/bin/major && \\
    sed -i 's/^     javac /     \\/opt\\/major-jdk\\/bin\\/javac /' /opt/major/bin/major && \\
    mkdir -p /opt/major-wrapper/classes && \\
    /opt/major-jdk/bin/javac -source 8 -target 8 -d /opt/major-wrapper/classes /opt/major-wrapper/Config.java && \\
    jar cf /opt/major-wrapper/major-runtime.jar -C /opt/major-wrapper/classes .

ENV MAJOR_HOME=/opt/major
ENV PATH=/opt/major/bin:$PATH

WORKDIR /workspace/repo
"""

    return f"""FROM eclipse-temurin:11-jdk AS major_jdk

FROM {target.mswe_image}

USER root

RUN rm -rf /workspace /home/{target.repo}
WORKDIR /workspace

COPY --from=major_jdk /opt/java/openjdk /opt/major-jdk
COPY major /opt/major
COPY major-wrapper /opt/major-wrapper
COPY repo /workspace/repo
COPY scripts /major-scripts

RUN chmod +x /opt/major/bin/* /opt/major-wrapper/* /major-scripts/*.sh && \\
    sed -i 's/^    javac /    \\/opt\\/major-jdk\\/bin\\/javac /' /opt/major/bin/major && \\
    sed -i 's/^     javac /     \\/opt\\/major-jdk\\/bin\\/javac /' /opt/major/bin/major && \\
    mkdir -p /opt/major-wrapper/classes && \\
    /opt/major-jdk/bin/javac -source 8 -target 8 -d /opt/major-wrapper/classes /opt/major-wrapper/Config.java && \\
    jar cf /opt/major-wrapper/major-runtime.jar -C /opt/major-wrapper/classes .

ENV MAJOR_HOME=/opt/major
ENV PATH=/opt/major/bin:$PATH

WORKDIR /workspace/repo
"""


def javac_wrapper() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

MAJOR_MML="${MAJOR_MML:-/opt/major/mml/all.mml.bin}"
MAJOR_JAR="/opt/major/lib/major.jar"

extra_args=()
if [[ -n "${MAJOR_EXTRA_ARGS:-}" ]]; then
  # Intentional shell-style splitting for advanced Major plugin options.
  read -r -a extra_args <<< "${MAJOR_EXTRA_ARGS}"
fi
if [[ -n "${MAJOR_REFACTOR_ARGS:-}" ]]; then
  extra_args+=(--refactor "${MAJOR_REFACTOR_ARGS}")
fi

if [[ -n "${MAJOR_DEBUG:-}" ]]; then
  mkdir -p /work
  {
    echo "ARGS:"
    printf '<%s>\n' "$@"
  } >> /work/major-wrapper-debug.log
fi

use_major=1
target_sources=()
if [[ -n "${MAJOR_TARGET_SOURCES:-}" ]]; then
  IFS=',' read -r -a target_sources <<< "${MAJOR_TARGET_SOURCES}"
fi

for arg in "$@"; do
  if [[ "${arg}" == *"target/test-classes"* ]]; then
    use_major=0
  fi
  if [[ "${arg}" == @* ]]; then
    args_file="${arg#@}"
    if [[ -f "${args_file}" ]]; then
      if grep -q "target/test-classes" "${args_file}"; then
        use_major=0
      fi
      if [[ -n "${MAJOR_DEBUG:-}" ]]; then
        {
          echo "ARGFILE BEFORE ${args_file}:"
          cat "${args_file}"
        } >> /work/major-wrapper-debug.log
      fi
      awk -v major_jar="${MAJOR_JAR}" '
        BEGIN { patch_next = 0; q = sprintf("%c", 34) }
        patch_next {
          cp = $0
          gsub(/^"|"$/, "", cp)
          haystack = ":" cp ":"
          needle = ":" major_jar ":"
          if (index(haystack, needle) == 0) {
            printf "%s%s:%s%s\\n", q, major_jar, cp, q
          } else {
            print $0
          }
          patch_next = 0
          next
        }
        {
          normalized = $0
          gsub(/^"|"$/, "", normalized)
          if (normalized == "-classpath" || normalized == "-cp" || normalized == "--class-path") {
            print q "-classpath" q
            patch_next = 1
            next
          }
          print $0
        }
      ' "${args_file}" > "${args_file}.major" && mv "${args_file}.major" "${args_file}"
      if [[ "${#target_sources[@]}" -gt 0 ]]; then
        awk -v targets="${MAJOR_TARGET_SOURCES}" '
          BEGIN { n = split(targets, target, ",") }
          {
            source = $0
            gsub(/^"|"$/, "", source)
            if (source ~ /\\.java$/) {
              keep = 0
              for (i = 1; i <= n; i++) {
                if (target[i] != "" && length(source) >= length(target[i]) && substr(source, length(source) - length(target[i]) + 1) == target[i]) {
                  keep = 1
                }
              }
              if (!keep) {
                next
              }
            }
            print $0
          }
        ' "${args_file}" > "${args_file}.targeted" && mv "${args_file}.targeted" "${args_file}"
      fi
      if [[ -n "${MAJOR_DEBUG:-}" ]]; then
        {
          echo "ARGFILE AFTER ${args_file}:"
          cat "${args_file}"
        } >> /work/major-wrapper-debug.log
      fi
    fi
  fi
done

if [[ "${use_major}" == "0" ]]; then
  exec javac "$@"
fi

exec /opt/major/bin/major --mml "${MAJOR_MML}" "${extra_args[@]}" "$@"
"""


def major_runtime_config() -> str:
    return """package major.mutation;

import java.io.File;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.BitSet;
import java.util.List;

public class Config {
    public static int __M_NO = Integer.getInteger("major.mutation.mutant", -1);
    public static BitSet covSet = new BitSet();

    static {
        Runtime.getRuntime().addShutdownHook(new Thread(new Runnable() {
            public void run() {
                String coverageFile = System.getProperty("major.coverage.file");
                if (coverageFile == null || coverageFile.length() == 0) {
                    return;
                }
                try {
                    File file = new File(coverageFile);
                    File parent = file.getParentFile();
                    if (parent != null) {
                        parent.mkdirs();
                    }
                    PrintWriter writer = new PrintWriter(file);
                    for (Integer mutant : getCoverageList()) {
                        writer.println(mutant.intValue());
                    }
                    writer.close();
                } catch (Throwable ignored) {
                }
            }
        }));
    }

    public Config() {
    }

    public static boolean COVERED(int from, int to) {
        synchronized (covSet) {
            covSet.set(from, to + 1);
        }
        return false;
    }

    public static void reset() {
        synchronized (covSet) {
            covSet.clear();
        }
    }

    public static List<Integer> getCoverageList() {
        synchronized (covSet) {
            List<Integer> covList = new ArrayList<Integer>(covSet.cardinality());
            for (int i = covSet.nextSetBit(0); i >= 0; i = covSet.nextSetBit(i + 1)) {
                covList.add(Integer.valueOf(i));
            }
            return covList;
        }
    }
}
"""


def run_major_script() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

cd /workspace/repo
mkdir -p /work

runtime_jar="/opt/major-wrapper/major-runtime.jar"
runtime_base="-Xbootclasspath/a:${runtime_jar}"
coverage_file="/work/coverage.txt"
details_file="/work/details.csv"
summary_file="/work/summary.json"
test_stdout_dir="/work/test-logs"
mkdir -p "${test_stdout_dir}"

echo "== Major environment =="
echo "PWD=$(pwd)"
echo "MAJOR_HOME=${MAJOR_HOME:-/opt/major}"
java -version
/opt/major/bin/major -version

if [[ -n "${MAJOR_SCOPES:-}" ]]; then
  scoped_mml="/work/targeted.mml"
  cat > "${scoped_mml}" <<'MML'
list_aor={+,-,*,/,%};
list_lor={&,|,^};
list_sor={<<,>>,>>>};
list_oru={+,-,~};

BIN(+)->list_aor;
BIN(-)->list_aor;
BIN(*)->list_aor;
BIN(/)->list_aor;
BIN(%)->list_aor;

BIN(>>)->list_sor;
BIN(<<)->list_sor;
BIN(>>>)->list_sor;

BIN(&)->list_lor;
BIN(|)->list_lor;
BIN(^)->list_lor;

UNR(+)->list_oru;
UNR(-)->list_oru;
UNR(~)->list_oru;

BIN(>)->{>=,!=,FALSE};
BIN(<)->{<=,!=,FALSE};
BIN(>=)->{>,==,TRUE};
BIN(<=)->{<,==,TRUE};
BIN(==)->{<=,>=,FALSE,LHS,RHS};
BIN(!=)->{<,>,TRUE,LHS,RHS};

BIN(&&)->{==,LHS,RHS,FALSE};
BIN(||)->{!=,LHS,RHS,TRUE};

DEL(CALL);
DEL(INC);
DEL(DEC);
DEL(ASSIGN);
DEL(CONT);
DEL(BREAK);
DEL(RETURN);
MML
  IFS=',' read -r -a scopes <<< "${MAJOR_SCOPES}"
  for scope in "${scopes[@]}"; do
    [[ -z "${scope}" ]] && continue
    {
      printf 'AOR<"%s">;\\n' "${scope}"
      printf 'LOR<"%s">;\\n' "${scope}"
      printf 'SOR<"%s">;\\n' "${scope}"
      printf 'COR<"%s">;\\n' "${scope}"
      printf 'ROR<"%s">;\\n' "${scope}"
      printf 'LVR<"%s">;\\n' "${scope}"
      printf 'ORU<"%s">;\\n' "${scope}"
      printf 'STD<"%s">;\\n' "${scope}"
    } >> "${scoped_mml}"
  done
  /opt/major-jdk/bin/java -jar /opt/major/lib/major.jar --mmlc "${scoped_mml}"
  export MAJOR_MML="${scoped_mml}.bin"
  echo "Using scoped MML ${MAJOR_MML} for ${MAJOR_SCOPES}"
fi

if [[ -z "${MAJOR_GENERATE_CMD:-}" ]]; then
  if [[ -f pom.xml ]]; then
    MAJOR_GENERATE_CMD='mvn test-compile -Dmaven.compiler.fork=true -Dmaven.compiler.executable=/opt/major-wrapper/javac -DargLine="-Xbootclasspath/a:/opt/major-wrapper/major-runtime.jar ${SUREFIRE_ARGLINE:-}" -DskipTests ${MAJOR_MAVEN_ARGS:-}'
  elif [[ -x ./gradlew ]]; then
    MAJOR_GENERATE_CMD='./gradlew testClasses --no-daemon --init-script /major-scripts/major-gradle-init.gradle ${MAJOR_GRADLE_ARGS:-}'
  else
    echo "No MAJOR_GENERATE_CMD and no supported build file found." >&2
    exit 2
  fi
fi

if [[ -z "${MAJOR_TEST_CMD:-}" ]]; then
  if [[ -f pom.xml ]]; then
    MAJOR_TEST_CMD='mvn surefire:test -DargLine="${MAJOR_ARGLINE}" ${MAJOR_TEST_ARGS:-}'
  elif [[ -x ./gradlew ]]; then
    MAJOR_TEST_CMD='./gradlew test --no-daemon ${MAJOR_TEST_ARGS:-}'
  else
    echo "No MAJOR_TEST_CMD and no supported build file found." >&2
    exit 2
  fi
fi

echo "== Generate mutants =="
echo "${MAJOR_GENERATE_CMD}"
bash -lc "${MAJOR_GENERATE_CMD}"

if [[ -n "${MAJOR_MANUAL_COMPILE:-}" ]]; then
  echo "== Targeted Major compile =="
  classes_dir="${MAJOR_CLASSES_DIR:-target/classes}"
  sourcepath="${MAJOR_SOURCEPATH:-src/main/java:target/generated-sources/annotations:}"
  generated_dir="${MAJOR_GENERATED_SOURCES_DIR:-target/generated-sources/annotations}"
  source_level="${MAJOR_SOURCE_LEVEL:-8}"
  target_level="${MAJOR_TARGET_LEVEL:-8}"
  encoding="${MAJOR_ENCODING:-UTF-8}"
  classpath_file="/work/major-classpath.txt"
  dependency_cmd="${MAJOR_DEPENDENCY_CLASSPATH_CMD:-}"
  if [[ -n "${dependency_cmd}" ]]; then
    bash -lc "${dependency_cmd}"
  else
    : > "${classpath_file}"
  fi
  dependency_cp="$(cat "${classpath_file}" 2>/dev/null || true)"
  compile_cp="/opt/major/lib/major.jar:${classes_dir}"
  if [[ -n "${dependency_cp}" ]]; then
    compile_cp="${compile_cp}:${dependency_cp}"
  fi
  IFS=',' read -r -a manual_sources <<< "${MAJOR_TARGET_SOURCE_FILES:-${MAJOR_TARGET_SOURCES:-}}"
  if [[ "${#manual_sources[@]}" -eq 0 || -z "${manual_sources[0]:-}" ]]; then
    echo "MAJOR_MANUAL_COMPILE is set but no MAJOR_TARGET_SOURCE_FILES are configured." >&2
    exit 4
  fi
  manual_extra_args=()
  if [[ -n "${MAJOR_EXTRA_ARGS:-}" ]]; then
    read -r -a manual_extra_args <<< "${MAJOR_EXTRA_ARGS}"
  fi
  if [[ -n "${MAJOR_REFACTOR_ARGS:-}" ]]; then
    manual_extra_args+=(--refactor "${MAJOR_REFACTOR_ARGS}")
  fi
  echo "Major sources: ${manual_sources[*]}"
  /opt/major/bin/major \
    --mml "${MAJOR_MML}" \
    "${manual_extra_args[@]}" \
    -d "${classes_dir}" \
    -classpath "${compile_cp}" \
    -sourcepath "${sourcepath}" \
    -s "${generated_dir}" \
    -g \
    -target "${target_level}" \
    -source "${source_level}" \
    -encoding "${encoding}" \
    "${manual_sources[@]}"
fi

echo "== Export generated mutant metadata =="
find . -name mutants.log -print > /work/mutants-log-locations.txt || true
first_mutants_log="$(head -n 1 /work/mutants-log-locations.txt || true)"
if [[ -z "${first_mutants_log}" || ! -f "${first_mutants_log}" ]]; then
  echo "No mutants.log produced." >&2
  exit 3
fi
cp "${first_mutants_log}" /work/mutants.log

echo "== Coverage run =="
rm -f "${coverage_file}"
export MAJOR_ARGLINE="${runtime_base} -Dmajor.mutation.mutant=0 -Dmajor.coverage.file=${coverage_file} ${MAJOR_EXTRA_TEST_JVM_ARGS:-}"
echo "${MAJOR_TEST_CMD}"
set +e
timeout "${MAJOR_TEST_TIMEOUT_SECONDS:-300}" bash -lc "${MAJOR_TEST_CMD}" > "${test_stdout_dir}/coverage.log" 2>&1
coverage_status=$?
set -e
if [[ "${coverage_status}" -ne 0 ]]; then
  echo "Coverage run failed with status ${coverage_status}; continuing with all generated mutants." >&2
fi

if [[ -s "${coverage_file}" ]]; then
  sort -n -u "${coverage_file}" > /work/mutants-to-run.txt
else
  cut -d: -f1 /work/mutants.log | sort -n -u > /work/mutants-to-run.txt
fi

if [[ -n "${MAJOR_MAX_MUTANTS:-}" ]]; then
  head -n "${MAJOR_MAX_MUTANTS}" /work/mutants-to-run.txt > /work/mutants-to-run.txt.limited
  mv /work/mutants-to-run.txt.limited /work/mutants-to-run.txt
fi

echo "== Mutation analysis =="
echo "mutant_id,status,exit_code,log_file" > "${details_file}"
total=0
killed=0
survived=0
timeout_count=0
while read -r mutant_id; do
  if [[ -z "${mutant_id}" ]]; then
    continue
  fi
  total=$((total + 1))
  log_file="${test_stdout_dir}/mutant-${mutant_id}.log"
  export MAJOR_ARGLINE="${runtime_base} -Dmajor.mutation.mutant=${mutant_id} ${MAJOR_EXTRA_TEST_JVM_ARGS:-}"
  set +e
  timeout "${MAJOR_TEST_TIMEOUT_SECONDS:-300}" bash -lc "${MAJOR_TEST_CMD}" > "${log_file}" 2>&1
  status_code=$?
  set -e
  if [[ "${status_code}" -eq 0 ]]; then
    status="SURVIVED"
    survived=$((survived + 1))
  elif [[ "${status_code}" -eq 124 ]]; then
    status="TIMEOUT"
    killed=$((killed + 1))
    timeout_count=$((timeout_count + 1))
  else
    status="KILLED"
    killed=$((killed + 1))
  fi
  echo "${mutant_id},${status},${status_code},${log_file}" >> "${details_file}"
done < /work/mutants-to-run.txt

score="$(awk -v killed="${killed}" -v total="${total}" 'BEGIN { if (total == 0) print "0.0"; else printf "%.16g", killed / total }')"
{
  echo "{"
  echo "  \"killed\": ${killed},"
  echo "  \"mutation_score\": ${score},"
  echo "  \"survived\": ${survived},"
  echo "  \"timeout\": ${timeout_count},"
  echo "  \"total\": ${total}"
  echo "}"
} > "${summary_file}"

echo "== Summary =="
cat "${summary_file}"
echo "Done."
"""

def gradle_init_script() -> str:
    return """allprojects {
    tasks.withType(JavaCompile).configureEach {
        options.fork = true
        options.forkOptions.executable = "/opt/major-wrapper/javac"
    }

    tasks.withType(Test).configureEach {
        jvmArgs "-Xbootclasspath/a:/opt/major-wrapper/major-runtime.jar"
    }
}
"""


def build_context(
    target: MajorTarget,
    checkout_dir: Path,
    major_home: Path,
    build_root: Path,
    include_git: bool,
    iterative_base_image: str | None,
) -> Path:
    checkout = checkout_dir / target.instance_id
    if not checkout.exists():
        raise FileNotFoundError(
            f"Bug checkout not found: {checkout}. Run `python run.py checkout-bug --specifics {target.instance_id}` first."
        )
    if not major_home.exists():
        raise FileNotFoundError(
            f"Major installation not found: {major_home}. Download it under data/major first."
        )

    context_dir = build_root / target.instance_id
    if context_dir.exists():
        shutil.rmtree(context_dir)
    context_dir.mkdir(parents=True, exist_ok=True)

    print(f"[context] copy checkout {checkout} -> {context_dir / 'repo'}")
    copy_tree(checkout, context_dir / "repo", include_git=include_git)

    print(f"[context] copy major {major_home} -> {context_dir / 'major'}")
    copy_tree(major_home, context_dir / "major", include_git=False)

    write_text(context_dir / "Dockerfile", dockerfile(target, iterative_base_image))
    write_text(context_dir / "major-wrapper" / "javac", javac_wrapper())
    write_text(context_dir / "major-wrapper" / "Config.java", major_runtime_config())
    write_text(context_dir / "scripts" / "run-major.sh", run_major_script())
    write_text(
        context_dir / "scripts" / "major-gradle-init.gradle",
        gradle_init_script(),
    )
    write_text(
        context_dir / "metadata.json",
        json.dumps(
            {
                "instance_id": target.instance_id,
                "parent_image": target.mswe_image,
                "major_image": target.major_image,
            },
            indent=2,
        )
        + "\n",
    )
    return context_dir


def major_build_main(args: argparse.Namespace) -> None:
    target = select_one_target(args)
    image = args.image or target.major_image
    iterative_base_image = image if docker_image_exists(image) else None
    if iterative_base_image:
        print(f"[docker-build] reusing existing Major image as build base: {iterative_base_image}")
    context_dir = build_context(
        target=target,
        checkout_dir=args.checkout_dir,
        major_home=args.major_home,
        build_root=args.build_root,
        include_git=args.include_git,
        iterative_base_image=iterative_base_image,
    )

    print(f"[docker-build] {image}")
    try:
        run_cmd(["docker", "build", "-t", image, "."], cwd=context_dir)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc
    print(f"Built image: {image}")
    print(f"Build context: {context_dir}")


def major_run_main(args: argparse.Namespace) -> None:
    target = select_one_target(args)
    image = args.image or target.major_image
    output_dir = args.output_dir or (DEFAULT_OUTPUT_ROOT / target.instance_id / "major")
    output_dir.mkdir(parents=True, exist_ok=True)

    command = args.cmd or "bash /major-scripts/run-major.sh"
    env_vars = default_major_env(target, args.tests)
    env_vars.extend(args.env or [])
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{output_dir.resolve()}:/work",
    ]
    if args.m2_cache_dir:
        args.m2_cache_dir.mkdir(parents=True, exist_ok=True)
        docker_cmd.extend(["-v", f"{args.m2_cache_dir.resolve()}:/root/.m2"])
    for env in env_vars:
        docker_cmd.extend(["-e", env])
    docker_cmd.extend([image, "bash", "-lc", command])

    print(f"[docker-run] {image}")
    print(f"[output] {output_dir}")
    try:
        subprocess.run(docker_cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc


def default_major_env(target: MajorTarget, tests: list[str] | None = None) -> list[str]:
    selected_tests = tuple(tests or target.f2p_tests)
    common_env = common_major_env(target)

    if target.org == "alibaba" and target.repo == "fastjson2":
        test_selector = ",".join(selected_tests) or "*Test"
        target_sources = ",".join(
            f"core/{source}" for source in target_source_suffixes(target)
        )
        return common_env + maven_env(
            generate="./mvnw -V --no-transfer-progress -pl core test-compile",
        test=(
            "./mvnw -V --no-transfer-progress -pl core surefire:test "
                f"-Dtest={test_selector} -DfailIfNoSpecifiedTests=false "
                "-DargLine=\"${MAJOR_ARGLINE}\""
            ),
            extra=[
                "MAJOR_MANUAL_COMPILE=1",
                f"MAJOR_TARGET_SOURCE_FILES={target_sources}",
                "MAJOR_CLASSES_DIR=core/target/classes",
                "MAJOR_SOURCEPATH=core/src/main/java:core/target/generated-sources/annotations:",
                "MAJOR_GENERATED_SOURCES_DIR=core/target/generated-sources/annotations",
                (
                    "MAJOR_DEPENDENCY_CLASSPATH_CMD="
                    "find /root/.m2/repository -name '*.jar' "
                    "| paste -sd: - > /work/major-classpath.txt"
                ),
            ],
            instrument_generate=False,
        )

    if target.org == "apache" and target.repo == "dubbo":
        test_selector = ",".join(selected_tests) or "*Test"
        return common_env + maven_env(
            generate="mvn test-compile -Dsurefire.useFile=false -DfailIfNoTests=false",
            test=(
                "mvn surefire:test -Dsurefire.useFile=false "
                f"-Dtest={test_selector} -DfailIfNoTests=false "
                "-DfailIfNoSpecifiedTests=false "
                "-DargLine=\"${MAJOR_ARGLINE}\""
            ),
        )

    if target.org == "fasterxml" and target.repo in {
        "jackson-core",
        "jackson-databind",
        "jackson-dataformat-xml",
    }:
        test_selector = ",".join(selected_tests) or "*Test"
        return common_env + maven_env(
            generate="mvn test-compile -DfailIfNoTests=false",
            test=(
                "mvn surefire:test "
                f"-Dtest={test_selector} -DfailIfNoTests=false "
                "-DfailIfNoSpecifiedTests=false "
                "-DargLine=\"${MAJOR_ARGLINE}\""
            ),
        )

    if target.org == "google" and target.repo == "gson":
        test_selector = ",".join(selected_tests) or "*Test"
        return common_env + maven_env(
            generate="mvn -pl gson test-compile -DskipTests",
            test=(
                "mvn -pl gson surefire:test "
                f"-Dtest={test_selector} -DfailIfNoSpecifiedTests=false "
                "-DargLine=\"${MAJOR_ARGLINE}\""
            ),
        )

    if target.org == "elastic" and target.repo == "logstash":
        task = gradle_task_from_tests(selected_tests, "logstash-core:javaTests")
        return common_env + gradle_env(generate="./gradlew classes testClasses --no-daemon", test=f"./gradlew {task} --no-daemon")

    if target.org == "googlecontainertools" and target.repo == "jib":
        task = gradle_task_from_tests(selected_tests, "jib-core:test")
        return common_env + gradle_env(generate="./gradlew classes testClasses --no-daemon", test=f"./gradlew {task} --no-daemon")

    if target.org == "mockito" and target.repo == "mockito":
        task = gradle_task_from_tests(selected_tests, "test")
        toolchain = (
            "-Dorg.gradle.java.installations.auto-detect=false "
            "-Dorg.gradle.java.installations.paths=/usr/lib/jvm/zulu21-ca-amd64"
        )
        return common_env + gradle_env(
            generate=f"./gradlew classes testClasses --no-daemon {toolchain}",
            test=f"./gradlew {task} --no-daemon {toolchain}",
        )

    return common_env


def common_major_env(target: MajorTarget) -> list[str]:
    env = []
    if target.mutation_scopes:
        env.append(f"MAJOR_SCOPES={','.join(target.mutation_scopes)}")
    sources = target_source_suffixes(target)
    if sources:
        env.append(f"MAJOR_TARGET_SOURCES={','.join(sources)}")
    return env


def target_source_suffixes(target: MajorTarget) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()
    for scope in target.mutation_scopes:
        class_scope = scope.split("@", 1)[0].split("::", 1)[0]
        source = "src/main/java/" + class_scope.replace(".", "/") + ".java"
        if source not in seen:
            sources.append(source)
            seen.add(source)
    return sources


def maven_env(
    generate: str,
    test: str,
    extra: list[str] | None = None,
    instrument_generate: bool = True,
) -> list[str]:
    compiler_args = (
        "-Dmaven.compiler.fork=true "
        "-Dmaven.compiler.executable=/opt/major-wrapper/javac "
        "-DargLine=\"-Xbootclasspath/a:/opt/major-wrapper/major-runtime.jar ${SUREFIRE_ARGLINE:-}\""
    )
    env = [
        f"MAJOR_GENERATE_CMD={generate} {compiler_args if instrument_generate else ''}".strip(),
        f"MAJOR_TEST_CMD={test}",
        "MAJOR_REFACTOR_ARGS=enable.method.refactor enable.decl.refactor",
    ]
    env.extend(extra or [])
    return env


def gradle_env(generate: str, test: str) -> list[str]:
    return [
        f"MAJOR_GENERATE_CMD={generate} --init-script /major-scripts/major-gradle-init.gradle",
        f"MAJOR_TEST_CMD={test}",
    ]


def gradle_task_from_tests(tests: tuple[str, ...], fallback: str) -> str:
    for test in tests:
        if ":" in test and ">" not in test:
            return test
    return fallback


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dataset-files",
        nargs="+",
        default=DEFAULT_DATASET_FILES,
        help="Dataset JSONL files or glob patterns.",
    )
    parser.add_argument(
        "--specifics",
        nargs="+",
        required=True,
        help="Exactly one instance filter, e.g. google__gson-1093.",
    )
    parser.add_argument(
        "--image",
        default="",
        help="Override the generated Major image name.",
    )


def add_major_build_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "major-build",
        help="Build a Major-enabled Docker image for one bug-version instance.",
    )
    add_common_args(parser)
    parser.add_argument(
        "--checkout-dir",
        type=Path,
        default=DEFAULT_CHECKOUT_DIR,
        help="Directory containing checkout_bug source trees.",
    )
    parser.add_argument(
        "--major-home",
        type=Path,
        default=DEFAULT_MAJOR_HOME,
        help="Local Major installation directory.",
    )
    parser.add_argument(
        "--build-root",
        type=Path,
        default=DEFAULT_BUILD_ROOT,
        help="Directory for generated Docker build contexts.",
    )
    parser.add_argument(
        "--include-git",
        action="store_true",
        help="Include .git in the copied source tree.",
    )
    parser.set_defaults(func=major_build_main)


def add_major_run_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "major-run",
        help="Run Major mutation testing in a previously built Major image.",
    )
    add_common_args(parser)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Host directory mounted as /work in the container.",
    )
    parser.add_argument(
        "--cmd",
        default="",
        help="Override the command executed in the container.",
    )
    parser.add_argument(
        "--tests",
        nargs="+",
        default=[],
        help=(
            "Override tests used by the built-in analyzer profile. "
            "Defaults to the instance's f2p_tests from the dataset."
        ),
    )
    parser.add_argument(
        "--env",
        nargs="*",
        default=[],
        help="Environment variables passed to docker run, e.g. KEY=value.",
    )
    parser.add_argument(
        "--m2-cache-dir",
        type=Path,
        default=None,
        help="Optional host Maven cache directory mounted as /root/.m2.",
    )
    parser.set_defaults(func=major_run_main)
