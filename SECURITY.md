# Security and privacy

This is a local personal tool, not a hosted service. The dashboard listens
on 127.0.0.1, validates the Host and Origin headers, and does not enable
cross-origin reads. Do not tunnel, reverse-proxy, or port-forward it.

Publishing this source does not open a listener on the author's computer.
Running a scan sends outbound HTTP requests to configured career boards;
those sites see your public IP and normal request metadata. Discovery also
contacts public board indexes and search services. There is no notification
channel or application submission feature.

Keep personal config, credentials, job history, SQLite files, logs, and
reports out of Git. The public template has an empty company list. This
repository starts with fresh history rather than importing the working
monitor's older commits. Public examples and screenshots should use
synthetic data.

Board text is escaped in generated reports and links are limited to HTTP
and HTTPS. These safeguards and tests reduce risk; they are not a guarantee
against every vulnerability. Install updates and only configure trusted
career-board URLs. Never put credentials in board URLs.

Report suspected vulnerabilities through GitHub private vulnerability
reporting when enabled; otherwise contact the maintainer before posting
sensitive reproduction details publicly.
