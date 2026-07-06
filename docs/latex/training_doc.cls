% training document template

\NeedsTeXFormat{LaTeX2e}
% Class name must match the filename (training_doc.cls) and the pandoc
% `documentclass: training_doc` in docs/pandoc/pdf.yaml, or LaTeX warns that the
% requested class does not match the one provided.
\ProvidesClass{training_doc}


% Load the Base Class
\LoadClassWithOptions{scrartcl}

% Begin Requirements
\RequirePackage{ifthen}

% set font encoding for PDFLaTeX, XeLaTeX, or LuaTeX
\usepackage{ifxetex,ifluatex}
\if\ifxetex T\else\ifluatex T\else F\fi\fi T%
  \usepackage{fontspec}
  % Pandoc's LaTeX template unconditionally emits \usepackage{lmodern}, a pdfTeX
  % Type1 font package. This document renders with xelatex + fontspec, which
  % already selects Latin Modern, so lmodern is redundant here and would only be
  % an extra build dependency (its .sty would have to be installed purely to be
  % ignored). Mark it as already-loaded so the template's later
  % \usepackage{lmodern} is a harmless no-op. The pdfTeX branch below still loads
  % lmodern for real, since there it is the actual font package.
  \@ifundefined{ver@lmodern.sty}{%
    \expandafter\def\csname ver@lmodern.sty\endcsname{0000/00/00}}{}
\else
  \usepackage[T1]{fontenc}
  \usepackage[utf8]{inputenc}
  \usepackage{lmodern}
\fi

% Set the Paper Size and margins
\RequirePackage{geometry}
\geometry{margin=1.0in}

\RequirePackage{needspace}

\RequirePackage{float}

\RequirePackage{caption}
\RequirePackage{subcaption}
\RequirePackage[section]{placeins}

\RequirePackage{graphicx}

\RequirePackage{amsmath}

% quotes in italics
\RequirePackage{etoolbox}
\AtBeginEnvironment{quote}{\itshape}

% set up headers
\RequirePackage{fancyhdr}
\pagestyle{fancy}
\renewcommand{\headrulewidth}{0pt}
\lhead{\@author}
\rhead{\@title}

% tighten list formatting - Pandoc's tightlist doesn't quite work
\RequirePackage{enumitem}
\setlist{nosep}

% Add Department Command
\def\@department{\relax}
\newcommand{\department}[1]{\gdef\@department{#1}}

% needed for squarepipe, used as alt l3 list item ident
\usepackage{amssymb}

% KOMA Options
\KOMAoptions{}
% \setcounter{secnumdepth}{0}
\addtokomafont{disposition}{\rmfamily}
\addtokomafont{disposition}{\mdseries}
\addtokomafont{descriptionlabel}{\rmfamily\scshape}
\renewcommand\labelitemii{$\circ$}
% untested, but should be a hollow square
\renewcommand\labelitemiii{\tiny$\square$}
\addtokomafont{paragraph}{\rmfamily\scshape}

% use small caps for emphasis, and italic for strong
\let\textitold\textit
\let\emphold\emph
\renewcommand{\emph}{\textsc}
\renewcommand{\textit}{\textsc}
\renewcommand{\textbf}{\textitold}

% Custom Title
\renewcommand{\maketitle}{
\noindent
\begin{minipage}[c]{0.2\textwidth}
  \vspace{0pt}
  \begin{center}
    \includegraphics[width=\textwidth]{logo}
  \end{center}
\end{minipage}\hfill%
\begin{minipage}[c]{0.75\textwidth}
  \vspace{0pt}
  {\huge \@title \par}
  \medskip
  {\large \@department\hspace{.5em}\textperiodcentered\hspace{.5em}\@author}
  \vfill
\end{minipage}\hfill
\vspace{2\baselineskip}
\thispagestyle{plain}
}

% required for PanDoc
\providecommand{\tightlist}{}

% how deep to go in toc
\setcounter{tocdepth}{3}

% start the body on a fresh page after the table of contents
\let\oldtableofcontents\tableofcontents
\renewcommand{\tableofcontents}{\oldtableofcontents\clearpage}

% widow/orphan protection: never leave the first/last line of a paragraph
% stranded alone at the top or bottom of a page.
\clubpenalty=10000
\widowpenalty=10000
\displaywidowpenalty=10000
\brokenpenalty=10000

% keep headings with the text that follows them, so a heading is never left
% alone at the foot of a page (needspace forces a page break if too few lines
% remain for the heading plus a couple of lines of its section).
%
% The \if@nobreak guard matters: LaTeX sets @nobreak true immediately after a
% sectioning heading, which normally glues a section heading to a subsection
% heading that follows it with no text in between. A bare \needspace injects a
% breakable penalty that would defeat that glue and strand, say, a "Features"
% heading at the foot of a page while its first subsection starts the next one.
% So we only reserve space when we are NOT right after another heading.
\let\oldsection\section
\renewcommand{\section}{\if@nobreak\else\needspace{6\baselineskip}\fi\oldsection}
\let\oldsubsection\subsection
\renewcommand{\subsection}{\if@nobreak\else\needspace{6\baselineskip}\fi\oldsubsection}
\let\oldparagraph\paragraph
\renewcommand{\paragraph}{\if@nobreak\else\needspace{3\baselineskip}\fi\oldparagraph}

\newcommand\sbullet[1][.5]{\mathbin{\vcenter{\hbox{\scalebox{#1}{$\bullet$}}}}}

% enable "max width=" in graphcis

\usepackage[export]{adjustbox}
