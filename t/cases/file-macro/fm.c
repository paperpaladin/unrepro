#include <stdio.h>

/* Irreproducible in exactly one way: the build directory, via __FILE__. */
int main(void)
{
	printf("fm compiled from %s\n", __FILE__);
	return 0;
}
