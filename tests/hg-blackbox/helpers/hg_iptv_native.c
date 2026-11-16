/* SPDX-License-Identifier: GPL-2.0-only */
/* Temporary native probes. No firmware package or raw packet socket required. */
#include <string.h>

int sample_main(int argc, char **argv);
int rtp_main(int argc, char **argv);
int diag_main(int argc, char **argv);
int pse_main(int argc, char **argv);
int health_main(int argc, char **argv);

int main(int argc, char **argv)
{
	if (argc < 2)
		return 2;
	if (!strcmp(argv[1], "sample"))
		return sample_main(argc - 1, argv + 1);
	if (!strcmp(argv[1], "rtp"))
		return rtp_main(argc - 1, argv + 1);
	if (!strcmp(argv[1], "diag"))
		return diag_main(argc - 1, argv + 1);
	if (!strcmp(argv[1], "pse"))
		return pse_main(argc - 1, argv + 1);
	if (!strcmp(argv[1], "health"))
		return health_main(argc - 1, argv + 1);
	return 2;
}
