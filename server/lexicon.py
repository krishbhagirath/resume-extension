"""Skills dictionary: canonical names, aliases, and the role categories each skill signals.

Used for deterministic job analysis, matching, and fact checking. Matching is
case-insensitive on word boundaries. Aliases cover spelling variants and common
abbreviations ("k8s" -> Kubernetes, "Postgres" -> PostgreSQL).
"""
from __future__ import annotations

import re
from functools import lru_cache

# Role categories (hybrid roles score on several)
ROLES = {
    "swe": "General Software Engineering",
    "backend": "Backend/Infrastructure Engineering",
    "frontend": "Frontend/Full-Stack Engineering",
    "mobile": "Mobile Engineering",
    "ml": "Machine Learning Engineering",
    "ai": "Applied AI / LLMs",
    "cv": "Computer Vision",
    "robotics": "Robotics Software",
    "embedded": "Embedded Software / Firmware",
    "hardware": "AI Hardware / Digital Design",
    "systems": "Systems Engineering",
    "data": "Data Engineering / Analytics",
}

# canonical: (aliases, kind, role categories)
# kind: lang | framework | tool | cloud | db | ml | hw | concept
SKILLS: dict[str, tuple[list[str], str, list[str]]] = {
    # ---- languages
    "Python": (["python3", "py"], "lang", ["swe", "ml", "data", "backend"]),
    "C++": (["cpp", "c plus plus", "modern c++", "c++17", "c++20"], "lang", ["swe", "embedded", "robotics", "systems"]),
    "C": (["c language", "embedded c", "ansi c", "c99"], "lang", ["embedded", "systems"]),
    "Java": ([], "lang", ["swe", "backend"]),
    "JavaScript": (["js", "ecmascript", "es6"], "lang", ["frontend", "swe"]),
    "TypeScript": (["ts"], "lang", ["frontend", "swe", "backend"]),
    "Go": (["golang"], "lang", ["backend", "systems"]),
    "Rust": ([], "lang", ["systems", "backend", "embedded"]),
    "C#": (["c sharp", "csharp", ".net"], "lang", ["swe", "backend"]),
    "Kotlin": ([], "lang", ["mobile"]),
    "Swift": ([], "lang", ["mobile"]),
    "SQL": (["sql queries"], "lang", ["data", "backend"]),
    "PHP": ([], "lang", ["backend"]),
    "Ruby": (["ruby on rails", "rails"], "lang", ["backend"]),
    "Scala": ([], "lang", ["data", "backend"]),
    "R": (["r language"], "lang", ["data"]),
    "MATLAB": (["matlab/simulink", "simulink"], "lang", ["embedded", "robotics", "data"]),
    "Verilog": ([], "lang", ["hardware"]),
    "SystemVerilog": (["system verilog"], "lang", ["hardware"]),
    "VHDL": ([], "lang", ["hardware"]),
    "Bash": (["shell scripting", "shell", "bash scripting"], "lang", ["systems", "backend"]),
    "HTML/CSS": (["html", "css", "html5", "css3"], "lang", ["frontend"]),
    "Assembly": (["assembly language", "arm assembly", "asm"], "lang", ["embedded", "systems"]),
    # ---- frontend / web
    "React": (["react.js", "reactjs"], "framework", ["frontend"]),
    "React Native": (["react-native"], "framework", ["mobile", "frontend"]),
    "Next.js": (["nextjs", "next js"], "framework", ["frontend"]),
    "Node.js": (["nodejs", "node"], "framework", ["backend", "frontend"]),
    "Express": (["express.js", "expressjs"], "framework", ["backend"]),
    "Vue": (["vue.js", "vuejs"], "framework", ["frontend"]),
    "Angular": ([], "framework", ["frontend"]),
    "Tailwind CSS": (["tailwind", "tailwindcss"], "framework", ["frontend"]),
    "Expo": ([], "framework", ["mobile"]),
    "Flutter": ([], "framework", ["mobile"]),
    "GraphQL": ([], "framework", ["backend", "frontend"]),
    "REST APIs": (["restful", "rest api", "restful apis", "rest/json", "api design"], "concept", ["backend"]),
    # ---- backend
    "FastAPI": ([], "framework", ["backend", "ml"]),
    "Flask": ([], "framework", ["backend"]),
    "Django": ([], "framework", ["backend"]),
    "Spring": (["spring boot", "springboot"], "framework", ["backend"]),
    "Laravel": ([], "framework", ["backend"]),
    "gRPC": ([], "framework", ["backend", "systems"]),
    "Kafka": (["apache kafka"], "tool", ["backend", "data"]),
    "RabbitMQ": ([], "tool", ["backend"]),
    "Redis": ([], "db", ["backend"]),
    "Microservices": (["microservice", "service-oriented"], "concept", ["backend"]),
    "Distributed Systems": (["distributed system", "distributed computing"], "concept", ["backend", "systems"]),
    "Webhooks": (["webhook"], "concept", ["backend"]),
    "Authentication": (["auth", "oauth", "oauth2", "authorization", "sso", "auth.js", "authjs"], "concept", ["backend"]),
    "Caching": (["cache", "caches"], "concept", ["backend"]),
    "Performance Optimization": (["latency", "throughput", "p95", "p99", "profiling", "performance tuning"], "concept", ["backend", "systems"]),
    # ---- data / db
    "PostgreSQL": (["postgres", "postgresql", "psql"], "db", ["backend", "data"]),
    "MySQL": ([], "db", ["backend", "data"]),
    "MongoDB": (["mongo"], "db", ["backend"]),
    "SQLite": ([], "db", ["backend"]),
    "Supabase": ([], "db", ["backend", "frontend"]),
    "Firebase": (["firestore"], "db", ["mobile", "backend"]),
    "Prisma": ([], "tool", ["backend"]),
    "Pandas": ([], "ml", ["data", "ml"]),
    "NumPy": (["numpy"], "ml", ["data", "ml"]),
    "Spark": (["apache spark", "pyspark"], "tool", ["data"]),
    "Airflow": (["apache airflow"], "tool", ["data"]),
    "ETL": (["data pipeline", "data pipelines", "etl pipelines"], "concept", ["data"]),
    "Data Visualization": (["dashboards", "matplotlib", "chart.js", "tableau", "power bi"], "concept", ["data", "frontend"]),
    # ---- cloud / devops
    "AWS": (["amazon web services", "s3", "ec2", "lambda", "aws s3"], "cloud", ["backend", "systems"]),
    "GCP": (["google cloud", "google cloud platform"], "cloud", ["backend"]),
    "Azure": (["microsoft azure"], "cloud", ["backend"]),
    "Docker": (["containers", "containerization", "dockerfile"], "tool", ["backend", "systems"]),
    "Kubernetes": (["k8s"], "tool", ["backend", "systems"]),
    "Terraform": ([], "tool", ["backend", "systems"]),
    "CI/CD": (["ci", "cd", "continuous integration", "continuous deployment", "ci pipelines"], "concept", ["backend", "swe"]),
    "GitHub Actions": (["github actions"], "tool", ["swe", "backend"]),
    "Git": (["version control", "gitlab"], "tool", ["swe"]),
    "Linux": (["unix", "ubuntu", "linux vm"], "tool", ["systems", "embedded", "backend"]),
    "Vercel": ([], "cloud", ["frontend"]),
    "Playwright": ([], "tool", ["swe", "frontend"]),
    "Testing": (["unit testing", "unit tests", "test automation", "integration tests", "regression tests", "regression suites", "pytest", "phpunit", "jest", "tdd"], "concept", ["swe"]),
    "Postman": ([], "tool", ["backend"]),
    # ---- ML / AI
    "Machine Learning": (["ml", "machine-learning"], "concept", ["ml"]),
    "Deep Learning": (["deep-learning", "neural networks", "neural network", "dnn"], "concept", ["ml"]),
    "PyTorch": (["torch"], "ml", ["ml", "cv"]),
    "TensorFlow": (["tf", "keras", "tensorflow/keras"], "ml", ["ml"]),
    "Scikit-learn": (["sklearn", "scikit learn", "sci-kit learn"], "ml", ["ml", "data"]),
    "XGBoost": (["gradient boosting", "lightgbm", "histgradientboostingclassifier"], "ml", ["ml", "data"]),
    "ONNX": (["onnx runtime"], "ml", ["ml", "hardware"]),
    "Transformers": (["transformer", "vision transformer", "vit", "deit", "bert", "clinicalbert", "self-attention"], "ml", ["ml", "ai"]),
    "CNNs": (["cnn", "convolutional neural network", "convolutional neural networks"], "ml", ["ml", "cv"]),
    "LSTM": (["rnn", "recurrent neural network"], "ml", ["ml"]),
    "Reinforcement Learning": (["rl", "ppo", "reinforcement-learning", "policy optimization"], "ml", ["ml", "robotics"]),
    "LLMs": (["llm", "large language model", "large language models", "gpt", "gemini", "claude", "cohere"], "ml", ["ai"]),
    "RAG": (["retrieval-augmented generation", "retrieval augmented generation", "semantic search", "vector search", "embeddings"], "ml", ["ai"]),
    "LangChain": (["langgraph"], "ml", ["ai"]),
    "AI Agents": (["agent", "agents", "agentic", "multi-agent"], "ml", ["ai"]),
    "NLP": (["natural language processing", "text classification"], "ml", ["ai", "ml"]),
    "Prompt Engineering": (["prompting"], "ml", ["ai"]),
    "MLOps": (["model serving", "model deployment", "inference", "model monitoring"], "concept", ["ml"]),
    "Hugging Face": (["huggingface", "hugging face transformers"], "ml", ["ml", "ai"]),
    "Forecasting": (["time series", "time-series", "demand forecasting"], "ml", ["ml", "data"]),
    "Computer Vision": (["image processing", "object detection", "image classification", "segmentation"], "ml", ["cv"]),
    "OpenCV": (["cv2"], "ml", ["cv", "robotics"]),
    "YOLO": (["yolov5", "yolov8"], "ml", ["cv"]),
    "MediaPipe": ([], "ml", ["cv"]),
    "Grad-CAM": (["gradcam", "saliency", "interpretability", "explainability"], "ml", ["ml", "cv"]),
    "Quantization": (["int8", "post-training quantization", "quantized"], "ml", ["hardware", "ml"]),
    "Statistics": (["statistical", "probability", "a/b testing", "hypothesis testing"], "concept", ["data", "ml"]),
    "Simulation": (["simulator", "simulators", "discrete-event", "monte carlo"], "concept", ["ml", "robotics", "systems"]),
    "Algorithms": (["data structures", "graph algorithms", "dijkstra", "a*", "k-d tree"], "concept", ["swe"]),
    # ---- robotics
    "ROS": (["ros2", "ros 2", "robot operating system"], "tool", ["robotics"]),
    "Gazebo": ([], "tool", ["robotics"]),
    "SLAM": (["localization and mapping"], "concept", ["robotics", "cv"]),
    "LiDAR": (["lidar"], "hw", ["robotics"]),
    "PID Control": (["pid", "control systems", "feedback control"], "concept", ["robotics", "embedded"]),
    "Drones": (["uav", "dronekit", "px4", "ardupilot", "sitl"], "concept", ["robotics"]),
    "Sensor Fusion": (["imu", "kalman filter"], "concept", ["robotics", "embedded"]),
    # ---- embedded / hardware
    "Embedded Systems": (["embedded", "embedded software", "embedded systems"], "concept", ["embedded"]),
    "Firmware": (["firmware development", "bare-metal", "bare metal"], "concept", ["embedded"]),
    "RTOS": (["freertos", "zephyr", "real-time operating system"], "tool", ["embedded"]),
    "Microcontrollers": (["mcu", "mcus", "microcontroller", "arm cortex", "cortex-m"], "hw", ["embedded"]),
    "STM32": (["stm32 hal", "stm32cube", "stm32cube.ai", "nucleo"], "hw", ["embedded"]),
    "ESP32": (["esp32-s3"], "hw", ["embedded"]),
    "Arduino": ([], "hw", ["embedded"]),
    "Raspberry Pi": (["raspberry pi", "rpi"], "hw", ["embedded", "robotics"]),
    "Jetson": (["jetson nano", "nvidia jetson"], "hw", ["embedded", "robotics", "cv"]),
    "MSP432": (["msp432e401y", "ti msp432"], "hw", ["embedded"]),
    "UART": (["serial communication", "usart", "rs-232"], "concept", ["embedded"]),
    "I2C": (["i²c", "i2c"], "concept", ["embedded"]),
    "SPI": ([], "concept", ["embedded"]),
    "CAN": (["can bus", "can-bus", "canbus"], "concept", ["embedded", "robotics"]),
    "GPIO": ([], "concept", ["embedded"]),
    "Interrupts": (["interrupt", "isr", "dma"], "concept", ["embedded"]),
    "Oscilloscope": (["logic analyzer", "multimeter", "hardware debugging"], "tool", ["embedded", "hardware"]),
    "TinyML": (["edge ml", "edge ai", "on-device ml"], "ml", ["embedded", "ml"]),
    "FPGA": (["fpgas", "de10", "xilinx", "vivado", "quartus"], "hw", ["hardware"]),
    "RTL Design": (["rtl", "register-transfer level", "digital design", "fsm", "finite state machine"], "concept", ["hardware"]),
    "Hardware Verification": (["cocotb", "verilator", "uvm", "testbench", "testbenches"], "tool", ["hardware"]),
    "Computer Architecture": (["accelerator", "accelerators", "mac array", "systolic array", "asic"], "concept", ["hardware"]),
    "Compilers": (["compiler", "llvm", "mlir"], "concept", ["systems", "hardware"]),
    "Operating Systems": (["os internals", "kernel", "multithreading", "concurrency"], "concept", ["systems"]),
    "Networking": (["tcp/ip", "tcp", "udp", "sockets", "wi-fi", "wifi", "bluetooth", "ble"], "concept", ["systems", "embedded"]),
    "IoT": (["internet of things"], "concept", ["embedded"]),
    "Twilio": ([], "tool", ["backend"]),
    "CAD": (["autodesk inventor", "solidworks", "fusion 360", "3d printing"], "tool", ["robotics"]),
}

# Plain words that mean nothing on their own for matching (job-ad filler)
STOPWORDS = set("""a an and are as at be been being but by can could did do does for from had has have how i if in into is it
its may me might more most must my no nor not of on or our ours out over own per shall should so such than that the their
them then there these they this those through to too under up upon us very via was we were what when where which while who
whom why will with within without would you your yours able about above across after again against all also am any around
because before below between both each few further here just only other same some still very etc ie eg new well strong
work working team teams role roles experience experiences skills skill ability abilities knowledge plus years year
including include includes using use used build building looking join company opportunity candidate candidates ideal
responsibilities requirements qualifications preferred required minimum basic bonus nice will""".split())


def _alias_pattern(term: str) -> str:
    esc = re.escape(term.lower())
    # Word boundaries that also work for "c++", ".net", "c#", "a*". A dot before the term
    # counts as part of a word ("node.js" must not match "js"); a dot after it only does when
    # a letter follows ("Python." still matches, "node.js" does not match "node").
    before = r"(?<![a-z0-9])" if term.startswith(".") else r"(?<![a-z0-9+#.])"
    return rf"{before}{esc}(?![a-z0-9+#]|\.[a-z0-9])"


@lru_cache(maxsize=1)
def _compiled() -> list[tuple[str, re.Pattern]]:
    out = []
    for canon, (aliases, _kind, _roles) in SKILLS.items():
        terms = {canon.lower(), *[a.lower() for a in aliases]}
        # Very short/ambiguous aliases only count in their exact case-insensitive form with boundaries.
        pat = "|".join(_alias_pattern(t) for t in sorted(terms, key=len, reverse=True))
        out.append((canon, re.compile(pat, re.I)))
    return out


# Single letters / words that are too ambiguous in prose unless written in a skills context.
_AMBIGUOUS = {"C", "R", "Go", "CAN", "SPI", "Spring", "Express", "Node.js", "Machine Learning", "Computer Vision",
              "Testing", "Caching", "Statistics", "Algorithms", "Simulation", "Authentication", "Git"}


def find_skills(text: str) -> dict[str, int]:
    """Return {canonical skill: count} found in text. Longer matches win, so "Tailwind CSS"
    counts once as Tailwind CSS and not also as CSS, and "React Native" isn't also React."""
    t = " " + (text or "") + " "
    spans = []  # (length, start, end, canon, matched text)
    for canon, pat in _compiled():
        for m in pat.finditer(t):
            spans.append((m.end() - m.start(), m.start(), m.end(), canon, m))
    spans.sort(key=lambda x: (-x[0], x[1]))
    taken = []
    found: dict[str, int] = {}
    for _length, start, end, canon, m in spans:
        if any(start < e and end > s for s, e in taken):
            continue
        if canon in _AMBIGUOUS and not _convincing(canon, t, m):
            continue
        taken.append((start, end))
        found[canon] = found.get(canon, 0) + 1
    return found


def _convincing(canon: str, text: str, m: re.Match) -> bool:
    return _filter_ambiguous(canon, text, None, m) > 0


def _filter_ambiguous(canon: str, text: str, pat: re.Pattern | None, only: re.Match | None = None) -> int:
    """Count only convincing mentions of ambiguous names (e.g. the language C, not 'C' in 'Section C')."""
    n = 0
    for m in ([only] if only else pat.finditer(text)):
        s = m.group(0)
        before = text[max(0, m.start() - 25):m.start()].lower()
        after = text[m.end():m.end() + 25].lower()
        if canon in ("C", "R", "Go"):
            # must be written exactly (capital) and appear in a list or next to "language"/"programming"
            if s != canon:
                continue
            listy = re.search(r"[,/(]\s*$", before) or re.match(r"^\s*[,/)]", after) or \
                re.search(r"(languages?|programming|proficien|experience (in|with))", before + after)
            if not listy:
                continue
        elif canon == "CAN":
            if s != "CAN" and s.lower() not in ("can bus", "can-bus", "canbus"):
                continue
        elif canon == "SPI":
            if s != "SPI":
                continue
        elif canon in ("Spring", "Express"):
            if s[0] != canon[0]:  # must be capitalised
                continue
        elif canon == "Node.js":
            if s.lower() == "node" and not re.search(r"(javascript|js|backend|express|npm)", before + after):
                continue
        elif canon == "Git":
            pass
        n += 1
    return n


def kind(skill: str) -> str:
    return SKILLS.get(skill, ([], "concept", []))[1]


def roles_for(skill: str) -> list[str]:
    return SKILLS.get(skill, ([], "concept", []))[2]


def canonical(name: str) -> str | None:
    """Map a free-form skill name (e.g. from the bank's skills list) to its canonical name."""
    found = find_skills(name)
    if not found:
        return None
    # prefer the longest canonical match
    return max(found, key=len)


WORD_RE = re.compile(r"[a-z][a-z0-9+#.\-]*[a-z0-9+#]|[a-z]")


def content_words(text: str) -> list[str]:
    """Lowercased meaningful words for keyword overlap scoring."""
    words = WORD_RE.findall((text or "").lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 2]
