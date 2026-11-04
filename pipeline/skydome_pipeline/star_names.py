"""What to call a star on the page: its proper name if it has one (IAU
list, the brighter stars), then its Bayer or Flamsteed designation, then its
HR number, and the constellation it's in (DECISIONS.md D-028).

BSC5's name field (columns 5-14): Flamsteed number, Bayer letter
(abbreviated, "Alp"), the Bayer superscript, the constellation ("Lyr").
"""

from __future__ import annotations

GREEK = {"Alp": "α", "Bet": "β", "Gam": "γ", "Del": "δ", "Eps": "ε", "Zet": "ζ", "Eta": "η", "The": "θ",
         "Iot": "ι", "Kap": "κ", "Lam": "λ", "Mu": "μ", "Nu": "ν", "Xi": "ξ", "Omi": "ο", "Pi": "π",
         "Rho": "ρ", "Sig": "σ", "Tau": "τ", "Ups": "υ", "Phi": "φ", "Chi": "χ", "Psi": "ψ", "Ome": "ω"}
SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")

CONSTELLATIONS = {
    "And": "Andromeda", "Ant": "Antlia", "Aps": "Apus", "Aqr": "Aquarius", "Aql": "Aquila", "Ara": "Ara",
    "Ari": "Aries", "Aur": "Auriga", "Boo": "Boötes", "Cae": "Caelum", "Cam": "Camelopardalis",
    "Cnc": "Cancer", "CVn": "Canes Venatici", "CMa": "Canis Major", "CMi": "Canis Minor",
    "Cap": "Capricornus", "Car": "Carina", "Cas": "Cassiopeia", "Cen": "Centaurus", "Cep": "Cepheus",
    "Cet": "Cetus", "Cha": "Chamaeleon", "Cir": "Circinus", "Col": "Columba", "Com": "Coma Berenices",
    "CrA": "Corona Australis", "CrB": "Corona Borealis", "Crv": "Corvus", "Crt": "Crater", "Cru": "Crux",
    "Cyg": "Cygnus", "Del": "Delphinus", "Dor": "Dorado", "Dra": "Draco", "Equ": "Equuleus",
    "Eri": "Eridanus", "For": "Fornax", "Gem": "Gemini", "Gru": "Grus", "Her": "Hercules",
    "Hor": "Horologium", "Hya": "Hydra", "Hyi": "Hydrus", "Ind": "Indus", "Lac": "Lacerta", "Leo": "Leo",
    "LMi": "Leo Minor", "Lep": "Lepus", "Lib": "Libra", "Lup": "Lupus", "Lyn": "Lynx", "Lyr": "Lyra",
    "Men": "Mensa", "Mic": "Microscopium", "Mon": "Monoceros", "Mus": "Musca", "Nor": "Norma",
    "Oct": "Octans", "Oph": "Ophiuchus", "Ori": "Orion", "Pav": "Pavo", "Peg": "Pegasus", "Per": "Perseus",
    "Phe": "Phoenix", "Pic": "Pictor", "Psc": "Pisces", "PsA": "Piscis Austrinus", "Pup": "Puppis",
    "Pyx": "Pyxis", "Ret": "Reticulum", "Sge": "Sagitta", "Sgr": "Sagittarius", "Sco": "Scorpius",
    "Scl": "Sculptor", "Sct": "Scutum", "Ser": "Serpens", "Sex": "Sextans", "Tau": "Taurus",
    "Tel": "Telescopium", "Tri": "Triangulum", "TrA": "Triangulum Australe", "Tuc": "Tucana",
    "UMa": "Ursa Major", "UMi": "Ursa Minor", "Vel": "Vela", "Vir": "Virgo", "Vol": "Volans",
    "Vul": "Vulpecula",
}

# IAU proper names by designation ("Alp Lyr"; "Alp1 Cen" where the
# superscript matters; "27 Tau" for Flamsteed-only stars)
PROPER = {
    "Alp And": "Alpheratz", "Bet And": "Mirach", "Gam1 And": "Almach",
    "Alp Aql": "Altair", "Bet Aql": "Alshain", "Gam Aql": "Tarazed", "Zet Aql": "Okab",
    "Alp Aqr": "Sadalmelik", "Bet Aqr": "Sadalsuud", "Gam Aqr": "Sadachbia", "Del Aqr": "Skat",
    "Eps Aqr": "Albali",
    "Alp Ari": "Hamal", "Bet Ari": "Sheratan", "Gam1 Ari": "Mesarthim",
    "Alp Aur": "Capella", "Bet Aur": "Menkalinan", "The Aur": "Mahasim", "Iot Aur": "Hassaleh",
    "Eps Aur": "Almaaz", "Eta Aur": "Haedus", "Zet Aur": "Saclateni",
    "Alp Boo": "Arcturus", "Bet Boo": "Nekkar", "Gam Boo": "Seginus", "Eps Boo": "Izar", "Eta Boo": "Muphrid",
    "Alp CMa": "Sirius", "Bet CMa": "Mirzam", "Gam CMa": "Muliphein", "Del CMa": "Wezen",
    "Eps CMa": "Adhara", "Eta CMa": "Aludra",
    "Alp CMi": "Procyon", "Bet CMi": "Gomeisa",
    "Alp Cnc": "Acubens", "Bet Cnc": "Tarf", "Gam Cnc": "Asellus Borealis", "Del Cnc": "Asellus Australis",
    "Alp2 CVn": "Cor Caroli", "Bet CVn": "Chara",
    "Alp2 Cap": "Algedi", "Bet1 Cap": "Dabih", "Gam Cap": "Nashira", "Del Cap": "Deneb Algedi",
    "Alp Car": "Canopus", "Bet Car": "Miaplacidus", "Eps Car": "Avior", "Iot Car": "Aspidiske",
    "Alp Cas": "Schedar", "Bet Cas": "Caph", "Del Cas": "Ruchbah", "Eps Cas": "Segin", "Eta Cas": "Achird",
    "Alp1 Cen": "Rigil Kentaurus", "Bet Cen": "Hadar", "The Cen": "Menkent",
    "Alp Cep": "Alderamin", "Bet Cep": "Alfirk", "Gam Cep": "Errai",
    "Alp Cet": "Menkar", "Bet Cet": "Diphda", "Omi Cet": "Mira",
    "Alp Col": "Phact", "Bet Col": "Wazn",
    "Alp Com": "Diadem",
    "Alp CrB": "Alphecca",
    "Alp Crv": "Alchiba", "Bet Crv": "Kraz", "Gam Crv": "Gienah", "Del Crv": "Algorab", "Eps Crv": "Minkar",
    "Alp Crt": "Alkes",
    "Alp1 Cru": "Acrux", "Bet Cru": "Mimosa", "Gam Cru": "Gacrux", "Del Cru": "Imai",
    "Alp Cyg": "Deneb", "Bet1 Cyg": "Albireo", "Gam Cyg": "Sadr", "Del Cyg": "Fawaris", "Eps Cyg": "Aljanah",
    "Alp Del": "Sualocin", "Bet Del": "Rotanev",
    "Alp Dra": "Thuban", "Bet Dra": "Rastaban", "Gam Dra": "Eltanin", "Del Dra": "Altais", "Zet Dra": "Aldhibah",
    "Eta Dra": "Athebyne", "Iot Dra": "Edasich", "Lam Dra": "Giausar", "Xi Dra": "Grumium",
    "Alp Equ": "Kitalpha",
    "Alp Eri": "Achernar", "Bet Eri": "Cursa", "Gam Eri": "Zaurak", "Del Eri": "Rana", "Eps Eri": "Ran",
    "The1 Eri": "Acamar",
    "Alp Gem": "Castor", "Bet Gem": "Pollux", "Gam Gem": "Alhena", "Del Gem": "Wasat", "Eps Gem": "Mebsuta",
    "Eta Gem": "Propus", "Mu Gem": "Tejat", "Xi Gem": "Alzirr",
    "Alp Gru": "Alnair", "Bet Gru": "Tiaki",
    "Alp1 Her": "Rasalgethi", "Bet Her": "Kornephoros", "Del Her": "Sarin",
    "Alp Hya": "Alphard",
    "Alp Leo": "Regulus", "Bet Leo": "Denebola", "Gam1 Leo": "Algieba", "Del Leo": "Zosma",
    "The Leo": "Chertan", "Zet Leo": "Adhafera", "Mu Leo": "Rasalas",
    "Alp Lep": "Arneb", "Bet Lep": "Nihal",
    "Alp2 Lib": "Zubenelgenubi", "Bet Lib": "Zubeneschamali", "Gam Lib": "Zubenelhakrabi",
    "Alp Lyr": "Vega", "Bet Lyr": "Sheliak", "Gam Lyr": "Sulafat",
    "Alp Oph": "Rasalhague", "Bet Oph": "Cebalrai", "Del Oph": "Yed Prior", "Eps Oph": "Yed Posterior",
    "Eta Oph": "Sabik", "Lam Oph": "Marfik",
    "Alp Ori": "Betelgeuse", "Bet Ori": "Rigel", "Gam Ori": "Bellatrix", "Del Ori": "Mintaka",
    "Eps Ori": "Alnilam", "Zet Ori": "Alnitak", "Kap Ori": "Saiph", "Lam Ori": "Meissa", "Iot Ori": "Hatysa",
    "Alp Pav": "Peacock",
    "Alp Peg": "Markab", "Bet Peg": "Scheat", "Gam Peg": "Algenib", "Eps Peg": "Enif", "Eta Peg": "Matar",
    "Zet Peg": "Homam", "Mu Peg": "Sadalbari",
    "Alp Per": "Mirfak", "Bet Per": "Algol", "Xi Per": "Menkib", "Omi Per": "Atik",
    "Alp Phe": "Ankaa",
    "Alp Psc": "Alrescha", "Eta Psc": "Alpherg",
    "Alp PsA": "Fomalhaut",
    "Zet Pup": "Naos", "Rho Pup": "Tureis",
    "Alp Sge": "Sham",
    "Alp Sgr": "Rukbat", "Bet1 Sgr": "Arkab Prior", "Gam2 Sgr": "Alnasl", "Del Sgr": "Kaus Media",
    "Eps Sgr": "Kaus Australis", "Zet Sgr": "Ascella", "Lam Sgr": "Kaus Borealis", "Pi Sgr": "Albaldah",
    "Sig Sgr": "Nunki",
    "Alp Sco": "Antares", "Bet1 Sco": "Acrab", "Del Sco": "Dschubba", "Eps Sco": "Larawag",
    "The Sco": "Sargas", "Kap Sco": "Girtab", "Lam Sco": "Shaula", "Pi Sco": "Fang", "Sig Sco": "Alniyat",
    "Tau Sco": "Paikauhale", "Ups Sco": "Lesath",
    "Alp Ser": "Unukalhai",
    "Alp Tau": "Aldebaran", "Bet Tau": "Elnath", "Gam Tau": "Prima Hyadum", "Del1 Tau": "Secunda Hyadum",
    "Eps Tau": "Ain", "Zet Tau": "Tianguan", "Eta Tau": "Alcyone",
    "17 Tau": "Electra", "19 Tau": "Taygeta", "20 Tau": "Maia", "23 Tau": "Merope", "27 Tau": "Atlas",
    "Alp TrA": "Atria", "Alp Tri": "Mothallah",
    "Alp UMa": "Dubhe", "Bet UMa": "Merak", "Gam UMa": "Phecda", "Del UMa": "Megrez", "Eps UMa": "Alioth",
    "Zet UMa": "Mizar", "Eta UMa": "Alkaid", "80 UMa": "Alcor", "Iot UMa": "Talitha",
    "Lam UMa": "Tania Borealis", "Mu UMa": "Tania Australis", "Nu UMa": "Alula Borealis",
    "Xi UMa": "Alula Australis", "Omi UMa": "Muscida",
    "Alp UMi": "Polaris", "Bet UMi": "Kochab", "Gam UMi": "Pherkad",
    "Lam Vel": "Suhail", "Del Vel": "Alsephina",
    "Alp Vir": "Spica", "Bet Vir": "Zavijava", "Gam Vir": "Porrima", "Del Vir": "Minelauva",
    "Eps Vir": "Vindemiatrix", "Zet Vir": "Heze",
    "Alp Vul": "Anser",
}


def describe(field: str, hr: int) -> tuple[str, str, str]:
    """(name, designation, constellation) from BSC5 columns 5-14.
    name is the proper name, else the designation, else "HR n"."""
    flam, bayer, sup, con = field[0:3].strip(), field[3:6].strip(), field[6:7].strip(), field[7:10].strip()
    keys = []
    if bayer:
        keys += [f"{bayer}{sup} {con}", f"{bayer} {con}"]
    if flam:
        keys.append(f"{flam} {con}")
    proper = next((PROPER[k] for k in keys if k in PROPER), "")
    if bayer in GREEK:
        desig = f"{GREEK[bayer]}{sup.translate(SUPERSCRIPT)} {con}"
    elif bayer:
        desig = f"{bayer}{sup} {con}"
    elif flam:
        desig = f"{flam} {con}"
    else:
        desig = f"HR {hr}"
    return proper or desig, desig, CONSTELLATIONS.get(con, "")
