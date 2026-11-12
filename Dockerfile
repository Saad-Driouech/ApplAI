FROM python:3.12-slim

WORKDIR /app

# System deps: pdflatex and xelatex for CV compilation, with Latin Modern fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    texlive-latex-base \
    texlive-latex-recommended \
    texlive-latex-extra \
    texlive-fonts-recommended \
    texlive-xetex \
    lmodern \
    fonts-lmodern \
    && rm -rf /var/lib/apt/lists/*

# FontAwesome 5 icons for CV templates. Debian only ships it in texlive-fonts-extra (1.7 GB), so
# install the CTAN package alone, pinned by checksum (a CTAN update fails the build; bump deliberately)
ARG FONTAWESOME5_SHA256=83c86c8a92d80e0b2c84af78a055d10798f8294f63b397ba8225351b9eaaa500
RUN python -c "import hashlib, sys, urllib.request; \
data = urllib.request.urlopen('https://mirrors.ctan.org/fonts/fontawesome5.zip', timeout=120).read(); \
hashlib.sha256(data).hexdigest() == '${FONTAWESOME5_SHA256}' or sys.exit('fontawesome5.zip checksum mismatch'); \
open('/tmp/fa5.zip', 'wb').write(data)" \
    && python -m zipfile -e /tmp/fa5.zip /tmp \
    && T=/usr/local/share/texmf FA=/tmp/fontawesome5 \
    && mkdir -p $T/tex/latex/fontawesome5 $T/fonts/tfm/public/fontawesome5 $T/fonts/type1/public/fontawesome5 \
       $T/fonts/enc/dvips/fontawesome5 $T/fonts/map/dvips/fontawesome5 $T/fonts/opentype/public/fontawesome5 \
    && cp $FA/tex/* $T/tex/latex/fontawesome5/ && cp $FA/tfm/* $T/fonts/tfm/public/fontawesome5/ \
    && cp $FA/type1/* $T/fonts/type1/public/fontawesome5/ && cp $FA/enc/* $T/fonts/enc/dvips/fontawesome5/ \
    && cp $FA/map/* $T/fonts/map/dvips/fontawesome5/ && cp $FA/opentype/* $T/fonts/opentype/public/fontawesome5/ \
    && mktexlsr && updmap-sys --enable Map=fontawesome5.map \
    && rm -rf /tmp/fa5.zip $FA

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Unprivileged runtime user (same UID as the n8n container)
RUN useradd --create-home --uid 1000 applai
USER applai

CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
