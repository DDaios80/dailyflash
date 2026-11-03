"""Country names and demonyms (lower-case, accent-free) for spotting
diplomatic titles in reservation comments: "Ambassador of India",
"Ambassador to Greece", "Indian Ambassador". A country is what separates a
real ambassador from the marketing sense of the word ("make her our
ambassador", "ambassador of <brand>") that the notes also use.
"""
from __future__ import annotations

COUNTRY_NAMES: tuple[str, ...] = (
    "afghanistan", "albania", "algeria", "andorra", "angola", "antigua and barbuda",
    "argentina", "armenia", "australia", "austria", "azerbaijan", "bahamas", "bahrain",
    "bangladesh", "barbados", "belarus", "belgium", "belize", "benin", "bhutan",
    "bolivia", "bosnia and herzegovina", "bosnia", "botswana", "brazil", "brunei",
    "bulgaria", "burkina faso", "burundi", "cabo verde", "cape verde", "cambodia",
    "cameroon", "canada", "central african republic", "chad", "chile", "china",
    "colombia", "comoros", "congo", "costa rica", "cote d'ivoire", "ivory coast",
    "croatia", "cuba", "cyprus", "czech republic", "czechia", "denmark", "djibouti",
    "dominica", "dominican republic", "ecuador", "egypt", "el salvador",
    "equatorial guinea", "eritrea", "estonia", "eswatini", "ethiopia", "fiji",
    "finland", "france", "gabon", "gambia", "georgia", "germany", "ghana", "greece",
    "hellas", "grenada", "guatemala", "guinea", "guinea-bissau", "guyana", "haiti",
    "holy see", "vatican", "honduras", "hungary", "iceland", "india", "indonesia",
    "iran", "iraq", "ireland", "israel", "italy", "jamaica", "japan", "jordan",
    "kazakhstan", "kenya", "kiribati", "korea", "south korea", "north korea", "kosovo",
    "kuwait", "kyrgyzstan", "laos", "latvia", "lebanon", "lesotho", "liberia", "libya",
    "liechtenstein", "lithuania", "luxembourg", "madagascar", "malawi", "malaysia",
    "maldives", "mali", "malta", "marshall islands", "mauritania", "mauritius",
    "mexico", "micronesia", "moldova", "monaco", "mongolia", "montenegro", "morocco",
    "mozambique", "myanmar", "namibia", "nauru", "nepal", "netherlands", "holland",
    "new zealand", "nicaragua", "niger", "nigeria", "north macedonia", "macedonia",
    "norway", "oman", "pakistan", "palau", "palestine", "panama", "papua new guinea",
    "paraguay", "peru", "philippines", "poland", "portugal", "qatar", "romania",
    "russia", "russian federation", "rwanda", "saint kitts and nevis", "saint lucia",
    "saint vincent and the grenadines", "samoa", "san marino",
    "sao tome and principe", "saudi arabia", "senegal", "serbia", "seychelles",
    "sierra leone", "singapore", "slovakia", "slovenia", "solomon islands", "somalia",
    "south africa", "south sudan", "spain", "sri lanka", "sudan", "suriname", "sweden",
    "switzerland", "syria", "taiwan", "tajikistan", "tanzania", "thailand",
    "timor-leste", "togo", "tonga", "trinidad and tobago", "tunisia", "turkey",
    "turkiye", "turkmenistan", "tuvalu", "uganda", "ukraine", "united arab emirates",
    "uae", "united kingdom", "uk", "great britain", "britain", "england", "scotland",
    # no bare "us": "she will be an ambassador to us" is the marketing sense
    "united states of america", "united states", "usa", "america", "uruguay",
    "uzbekistan", "vanuatu", "venezuela", "vietnam", "viet nam", "yemen", "zambia",
    "zimbabwe", "european union", "eu",
)

DEMONYMS: tuple[str, ...] = (
    "afghan", "albanian", "algerian", "american", "angolan", "argentine", "argentinian",
    "armenian", "australian", "austrian", "azerbaijani", "bahraini", "bangladeshi",
    "belarusian", "belgian", "bolivian", "bosnian", "brazilian", "british", "bulgarian",
    "cambodian", "cameroonian", "canadian", "chilean", "chinese", "colombian",
    "congolese", "croatian", "cuban", "cypriot", "czech", "danish", "dutch",
    "ecuadorian", "egyptian", "emirati", "english", "estonian", "ethiopian", "filipino",
    "finnish", "french", "georgian", "german", "ghanaian", "greek", "guatemalan",
    "hungarian", "icelandic", "indian", "indonesian", "iranian", "iraqi", "irish",
    "israeli", "italian", "jamaican", "japanese", "jordanian", "kazakh", "kenyan",
    "korean", "kosovar", "kuwaiti", "latvian", "lebanese", "libyan", "lithuanian",
    "luxembourgish", "malaysian", "maltese", "mexican", "moldovan", "mongolian",
    "montenegrin", "moroccan", "nepalese", "nigerian", "norwegian", "omani",
    "pakistani", "palestinian", "panamanian", "paraguayan", "peruvian", "polish",
    "portuguese", "qatari", "romanian", "russian", "rwandan", "saudi", "scottish",
    "senegalese", "serbian", "singaporean", "slovak", "slovenian", "somali",
    "spanish", "sri lankan", "sudanese", "swedish", "swiss", "syrian", "taiwanese",
    "tanzanian", "thai", "tunisian", "turkish", "ugandan", "ukrainian", "uruguayan",
    "uzbek", "venezuelan", "vietnamese", "yemeni", "zambian", "zimbabwean",
)
