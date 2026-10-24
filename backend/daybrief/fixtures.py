from datetime import timedelta

from .models import Article

STORIES = [
    (
        "Open compute benchmark released",
        "technology",
        "Researchers released an open compute benchmark for evaluating inference efficiency.",
        90,
        "normal",
    ),
    (
        "Ocean monitoring network expands",
        "science",
        "Scientists deployed a new ocean monitoring network to measure water temperatures.",
        85,
        "normal",
    ),
    (
        "Flood evacuation order issued",
        "public_safety",
        "Local authorities issued an evacuation order after flooding damaged roads.",
        95,
        "sensitive",
    ),
    (
        "Experimental battery announced",
        "energy",
        "A startup announced an experimental battery prototype without independent testing.",
        40,
        "normal",
    ),
    ("Old satellite launch", "space", "A satellite launched last week.", 30, "normal"),
]


PARAPHRASES = {
    "A central bank held its benchmark interest rate unchanged after its policy meeting.": (
        "Following its policy meeting, a central bank kept the benchmark interest rate unchanged."
    ),
    "The national statistics agency reported slower annual consumer price inflation in its monthly release.": (
        "The monthly release from the national statistics agency showed annual consumer price inflation slowing."
    ),
    "The government announced emergency fiscal support for regions affected by severe flooding.": (
        "Regions affected by severe flooding will receive emergency fiscal support announced by the government."
    ),
    "Researchers released an open compute benchmark for evaluating inference efficiency.": (
        "A research group published an open compute benchmark that evaluates inference efficiency."
    ),
    "Scientists deployed a new ocean monitoring network to measure water temperatures.": (
        "A new ocean monitoring network was deployed by scientists to measure water temperatures."
    ),
    "Local authorities issued an evacuation order after flooding damaged roads.": (
        "Officials put an evacuation order in place after floods damaged local roads."
    ),
    "A research team published a dataset for evaluating language model reliability.": (
        "Researchers released a dataset used to evaluate language-model reliability."
    ),
    "Regulators opened a safety investigation after a data center fire injured workers.": (
        "A safety investigation was opened by regulators after workers were injured in a data center fire."
    ),
    "Astronomers released a telescope survey dataset for studying distant galaxies.": (
        "A telescope survey dataset for distant-galaxy research was released by astronomers."
    ),
    "Researchers paused a clinical trial after participants reported serious adverse effects.": (
        "A clinical trial was paused after participants reported serious adverse effects."
    ),
}


def articles(topic, now):
    output = []
    for i, (title, category, text, score, sensitivity) in enumerate(
        TOPIC_STORIES[topic]
    ):
        for j in range(1 if i in (3, 4) else 2):
            output.append(
                Article(
                    id=f"a{i}-{j}",
                    title=title,
                    url=f"https://publisher{j + 1}.example/story/{i}",
                    publisher=f"Publisher {j + 1}",
                    published_at=now - timedelta(hours=80 if i == 4 else 2 + i),
                    retrieved_at=now,
                    snippet=text if j == 0 else PARAPHRASES.get(text, text),
                    topic=topic,
                )
            )
    return output


TOPIC_STORIES = {
    "markets": [
        (
            "Central bank announces interest rate decision",
            "monetary_policy",
            "A central bank held its benchmark interest rate unchanged after its policy meeting.",
            90,
            "normal",
        ),
        (
            "Statistics agency releases monthly inflation data",
            "economic_data",
            "The national statistics agency reported slower annual consumer price inflation in its monthly release.",
            85,
            "normal",
        ),
        (
            "Government announces emergency fiscal support",
            "fiscal_policy",
            "The government announced emergency fiscal support for regions affected by severe flooding.",
            95,
            "sensitive",
        ),
        (
            "Unverified recession forecast circulates",
            "economic_outlook",
            "An economist predicted a recession without providing independently verified supporting evidence.",
            40,
            "normal",
        ),
        (
            "Central bank policy meeting held last week",
            "monetary_policy",
            "A central bank left its benchmark interest rate unchanged last week.",
            30,
            "normal",
        ),
    ],
    "general": STORIES,
    "ai_technology": [
        STORIES[0],
        (
            "AI model evaluation dataset published",
            "ai_research",
            "A research team published a dataset for evaluating language model reliability.",
            85,
            "normal",
        ),
        (
            "Data center safety investigation opened",
            "infrastructure",
            "Regulators opened a safety investigation after a data center fire injured workers.",
            95,
            "sensitive",
        ),
        (
            "Unverified AI performance claim",
            "products",
            "A startup claimed a performance improvement without publishing independent benchmarks.",
            40,
            "normal",
        ),
        STORIES[4],
    ],
    "science": [
        STORIES[1],
        (
            "Telescope survey dataset released",
            "astronomy",
            "Astronomers released a telescope survey dataset for studying distant galaxies.",
            85,
            "normal",
        ),
        (
            "Clinical trial safety review begins",
            "health",
            "Researchers paused a clinical trial after participants reported serious adverse effects.",
            95,
            "sensitive",
        ),
        (
            "Preliminary material discovery announced",
            "materials",
            "Researchers announced a preliminary material discovery awaiting independent replication.",
            40,
            "normal",
        ),
        STORIES[4],
    ],
}
