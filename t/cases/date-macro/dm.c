#include <stdio.h>

/* Irreproducible in exactly one way: the build clock, via the preprocessor. */
int main(void)
{
	printf("dm built %s %s\n", __DATE__, __TIME__);
	return 0;
}
