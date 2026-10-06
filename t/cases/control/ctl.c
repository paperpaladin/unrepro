#include <stdio.h>

/* No __DATE__, no __TIME__, no __FILE__. Nothing here knows when or where it
   was compiled, which is the entire trick. */
int main(void)
{
	printf("ctl\n");
	return 0;
}
